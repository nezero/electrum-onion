# Copyright (C) 2018 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

import asyncio
import os
from decimal import Decimal
import random
import time
from typing import (Optional, Sequence, Tuple, List, Set, Dict, TYPE_CHECKING,
                    NamedTuple, Union, Mapping, Any, Iterable)
import threading
import socket
import aiohttp
import json
from datetime import datetime, timezone
from functools import partial
from collections import defaultdict
import concurrent
from concurrent import futures
import urllib.parse

import dns.resolver
import dns.exception
from aiorpcx import run_in_thread, TaskGroup, NetAddress, ignore_after

from . import constants, util
from . import keystore
from .util import profiler, chunks
from .invoices import PR_TYPE_LN, PR_UNPAID, PR_EXPIRED, PR_PAID, PR_INFLIGHT, PR_FAILED, PR_ROUTING, LNInvoice, LN_EXPIRY_NEVER
from .util import NetworkRetryManager, JsonRPCClient
from .lnutil import LN_MAX_FUNDING_SAT
from .keystore import BIP32_KeyStore
from .bitcoin import COIN
from .bitcoin import opcodes, make_op_return, address_to_scripthash
from .transaction import Transaction
from .transaction import get_script_type_from_output_script
from .crypto import sha256
from .bip32 import BIP32Node
from .util import bh2u, bfh, InvoiceError, resolve_dns_srv, is_ip_address, log_exceptions
from .crypto import chacha20_encrypt, chacha20_decrypt
from .util import ignore_exceptions, make_aiohttp_session, SilentTaskGroup
from .util import timestamp_to_datetime, random_shuffled_copy
from .util import MyEncoder, is_private_netaddress
from .logging import Logger
from .lntransport import LNTransport, LNResponderTransport, LNTransportBase
from .lnpeer import Peer, LN_P2P_NETWORK_TIMEOUT
from .lnaddr import lnencode, LnAddr, lndecode
from .ecc import der_sig_from_sig_string
from .lnchannel import Channel, AbstractChannel
from .lnchannel import ChannelState, PeerState
from .lnrater import LNRater
from . import lnutil
from .lnutil import funding_output_script
from .bitcoin import redeem_script_to_address
from .lnutil import (Outpoint, LNPeerAddr,
                     get_compressed_pubkey_from_bech32, extract_nodeid,
                     PaymentFailure, split_host_port, ConnStringFormatError,
                     generate_keypair, LnKeyFamily, LOCAL, REMOTE,
                     MIN_FINAL_CLTV_EXPIRY_FOR_INVOICE,
                     NUM_MAX_EDGES_IN_PAYMENT_PATH, SENT, RECEIVED, HTLCOwner,
                     UpdateAddHtlc, Direction, LnFeatures, ShortChannelID,
                     HtlcLog, derive_payment_secret_from_payment_preimage,
                     NoPathFound, InvalidGossipMsg)
from .lnutil import ln_dummy_address, ln_compare_features, IncompatibleLightningFeatures
from .lnrouter import TrampolineEdge
from .transaction import PartialTxOutput, PartialTransaction, PartialTxInput
from .lnonion import OnionFailureCode, OnionRoutingFailure
from .lnmsg import decode_msg
from .i18n import _
from .lnrouter import (RouteEdge, LNPaymentRoute, LNPaymentPath, is_route_sane_to_use,
                       NoChannelPolicy, LNPathInconsistent)
from .address_synchronizer import TX_HEIGHT_LOCAL
from . import lnsweep
from .lnwatcher import LNWalletWatcher
from .crypto import pw_encode_with_version_and_mac, pw_decode_with_version_and_mac
from .lnutil import ImportedChannelBackupStorage, OnchainChannelBackupStorage
from .lnchannel import ChannelBackup
from .channel_db import UpdateStatus
from .channel_db import get_mychannel_info, get_mychannel_policy
from .submarine_swaps import SwapManager
from .channel_db import ChannelInfo, Policy
from .mpp_split import suggest_splits
from .trampoline import create_trampoline_route_and_onion, TRAMPOLINE_FEES

if TYPE_CHECKING:
    from .network import Network
    from .wallet import Abstract_Wallet
    from .channel_db import ChannelDB
    from .simple_config import SimpleConfig


SAVED_PR_STATUS = [PR_PAID, PR_UNPAID] # status that are persisted


NUM_PEERS_TARGET = 4

# onchain channel backup data
CB_VERSION = 0
CB_MAGIC_BYTES = bytes([0, 0, 0, CB_VERSION])


FALLBACK_NODE_LIST_TESTNET = (
)

FALLBACK_NODE_LIST_MAINNET = [
    LNPeerAddr(host='207.180.206.221', port=9735, pubkey=bfh('020a2af32eac60426bb360a8ed413194d371c35a17c1813fca0eab2c927884ddab')),
    LNPeerAddr(host='178.128.97.48', port=21002, pubkey=bfh('0211eeda84950d7078aa62383c7b91def5cf6c5bb52d209a324cda0482dbfbe4d2')),
    LNPeerAddr(host='54.184.240.102', port=9736, pubkey=bfh('0221bfef0d6019f3ab339c0a1ba3faa97627bcafa1bb371bb48d793960fa4a50fc')),
    LNPeerAddr(host='5.196.89.16', port=9735, pubkey=bfh('0268b9295227e5e1f751e510c47d1a13546282d4182dd121ef2fe5b53ea513fcf1')),
    LNPeerAddr(host='197.155.6.164', port=9735, pubkey=bfh('027c2986cd45217cd8c996555f4f0925fc01b3030cebfa6d59e90e230bb00b3a8b')),
    LNPeerAddr(host='14.203.57.50', port=9735, pubkey=bfh('02847102bd4c9fda696d523e100c3656d93e760302c06636b1cd0fbdf2b3fe7abc')),
    LNPeerAddr(host='118.163.74.161', port=10735, pubkey=bfh('02856e66429bf6b57a62df10a793b48093434aa7c40baa5f92b601afbdc9592ec6')),
    LNPeerAddr(host='104.196.200.39', port=10735, pubkey=bfh('02a4cb9d9c40ab508be3641a3b42be249e7cacfc7fea600485f9e37e46382aaa49')),
    LNPeerAddr(host='96.9.220.104', port=9735, pubkey=bfh('02e1c6cf76339e816af51ef32dfce138632e14392750f139c7d876b4535c37a1c8')),
    LNPeerAddr(host='54.157.91.178', port=9735, pubkey=bfh('02e2fc5f8c8a1003131f9a182f7bec328bd8b877c13a9c318851e49a737137195c')),
    LNPeerAddr(host='213.136.92.251', port=9735, pubkey=bfh('02e6e00d443531e8fea1e119db8eecc89ce043e26bc8f1002d26fb936d315b1127')),
    LNPeerAddr(host='89.245.95.112', port=9736, pubkey=bfh('02ea29f01498acaa8f71c50459e5d7efa76a4f515c45121f05ae389643b14dec1b')),
    LNPeerAddr(host='z75k6k5zeznj6j63.onion', port=9735, pubkey=bfh('02fadef5a8a223d2bd693c50ff64deceb7e09938f0695080771c21ec802190f011')),
    LNPeerAddr(host='92.53.89.123', port=9736, pubkey=bfh('035a6532c0e996f2fb9db3e96759148c4a13cf3d802a33f91ae5d4319be3d4686b')),
    LNPeerAddr(host='goojs5mqqnxtvlb2.onion', port=9735, pubkey=bfh('035f28ec90cdcfb428f96486aeb2437d0cc49812b92595d51ccbf5ecf7dea7f9d9')),
    LNPeerAddr(host='170.75.162.173', port=9736, pubkey=bfh('036f4aa2256e52b9e1d93a4a04ceccfbc895c43b522287040a47899d20cea9c7f3')),
    LNPeerAddr(host='155.138.140.93', port=9735, pubkey=bfh('0371542b7780cf69220924a16ce5a0554b7128ad96a9dc8955879ca23a35f7111e')),
    LNPeerAddr(host='70.63.170.86', port=9735, pubkey=bfh('0371be28c68cb78b8f4be20e81458809ce89f3f77270efab80e08ee113e5651381')),
    LNPeerAddr(host='178.128.97.48', port=11002, pubkey=bfh('0375e7d882b442785aa697d57c3ed3aef523eb2743193389bd205f9ae0c609e6f3')),
    LNPeerAddr(host='35.157.75.94', port=9735, pubkey=bfh('037fc85cbe4e67da6ce3107e5489d029b67339beba73e857743952d85cb806f1e9')),
    LNPeerAddr(host='23.237.77.18', port=9735, pubkey=bfh('039f978f3a0d40783ab3436bb3df3345e045a9fe92f7244122da0b7a9d90cb28d5')),
    LNPeerAddr(host='78.20.92.179', port=9735, pubkey=bfh('03a223d51deabe4d1b6540150d0230305ef0948b37c175e1b305eaf40005442a94')),
    LNPeerAddr(host='185.183.85.137', port=9735, pubkey=bfh('03bd28308eac53495ec26adfb8d22ab29d7b52a379965a1dcd826a1be405fe687e')),
    LNPeerAddr(host='10.15.6.187', port=9735, pubkey=bfh('03e708a833c892d7acdfb9a52f033997b0351502c2aaa8c680eb20e4091fb07482')),
    LNPeerAddr(host='92.139.138.169', port=9735, pubkey=bfh('03f0cdfced02f14a68107b0ab891cd6e89834c0f5142151611c4990250a2bacd1c')),
    LNPeerAddr(host='34.193.68.10', port=9735, pubkey=bfh('03ff15472d52799a435d669414695e7f18d343b7a8a51164c5f67c5f52ff1c185d')),
]


from .trampoline import trampolines_by_id, hardcoded_trampoline_nodes, is_hardcoded_trampoline


class PaymentInfo(NamedTuple):
    payment_hash: bytes
    amount_msat: Optional[int]
    direction: int
    status: int


class ErrorAddingPeer(Exception): pass


# set some feature flags as baseline for both LNWallet and LNGossip
# note that e.g. DATA_LOSS_PROTECT is needed for LNGossip as many peers require it
BASE_FEATURES = LnFeatures(0)\
    | LnFeatures.OPTION_DATA_LOSS_PROTECT_OPT\
    | LnFeatures.OPTION_STATIC_REMOTEKEY_OPT\
    | LnFeatures.VAR_ONION_OPT\
    | LnFeatures.PAYMENT_SECRET_OPT\
    | LnFeatures.OPTION_UPFRONT_SHUTDOWN_SCRIPT_OPT

# we do not want to receive unrequested gossip (see lnpeer.maybe_save_remote_update)
LNWALLET_FEATURES = BASE_FEATURES\
    | LnFeatures.OPTION_DATA_LOSS_PROTECT_REQ\
    | LnFeatures.OPTION_STATIC_REMOTEKEY_REQ\
    | LnFeatures.GOSSIP_QUERIES_REQ\
    | LnFeatures.BASIC_MPP_OPT\
    | LnFeatures.OPTION_TRAMPOLINE_ROUTING_OPT

LNGOSSIP_FEATURES = BASE_FEATURES\
    | LnFeatures.GOSSIP_QUERIES_OPT\
    | LnFeatures.GOSSIP_QUERIES_REQ


class LNWorker(Logger, NetworkRetryManager[LNPeerAddr]):

    INITIAL_TRAMPOLINE_FEE_LEVEL = 1 # only used for trampoline payments. set to 0 in tests.

    def __init__(self, xprv, features: LnFeatures):
        Logger.__init__(self)
        NetworkRetryManager.__init__(
            self,
            max_retry_delay_normal=3600,
            init_retry_delay_normal=600,
            max_retry_delay_urgent=300,
            init_retry_delay_urgent=4,
        )
        self.lock = threading.RLock()
        self.node_keypair = generate_keypair(BIP32Node.from_xkey(xprv), LnKeyFamily.NODE_KEY)
        self.backup_key = generate_keypair(BIP32Node.from_xkey(xprv), LnKeyFamily.BACKUP_CIPHER).privkey
        self._peers = {}  # type: Dict[bytes, Peer]  # pubkey -> Peer  # needs self.lock
        self.taskgroup = SilentTaskGroup()
        self.listen_server = None  # type: Optional[asyncio.AbstractServer]
        self.features = features
        self.network = None  # type: Optional[Network]
        self.config = None  # type: Optional[SimpleConfig]
        self.stopping_soon = False  # whether we are being shut down

        util.register_callback(self.on_proxy_changed, ['proxy_set'])

    @property
    def channel_db(self):
        return self.network.channel_db if self.network else None

    @property
    def peers(self) -> Mapping[bytes, Peer]:
        """Returns a read-only copy of peers."""
        with self.lock:
            return self._peers.copy()

    def channels_for_peer(self, node_id: bytes) -> Dict[bytes, Channel]:
        return {}

    def get_node_alias(self, node_id: bytes) -> Optional[str]:
        """Returns the alias of the node, or None if unknown."""
        node_alias = None
        if self.channel_db:
            node_info = self.channel_db.get_node_info_for_node_id(node_id)
            if node_info:
                node_alias = node_info.alias
        else:
            for k, v in hardcoded_trampoline_nodes().items():
                if v.pubkey == node_id:
                    node_alias = k
                    break
        return node_alias

    async def maybe_listen(self):
        # FIXME: only one LNWorker can listen at a time (single port)
        listen_addr = self.config.get('lightning_listen')
        if listen_addr:
            self.logger.info(f'lightning_listen enabled. will try to bind: {listen_addr!r}')
            try:
                netaddr = NetAddress.from_string(listen_addr)
            except Exception as e:
                self.logger.error(f"failed to parse config key 'lightning_listen'. got: {e!r}")
                return
            addr = str(netaddr.host)
            async def cb(reader, writer):
                transport = LNResponderTransport(self.node_keypair.privkey, reader, writer)
                try:
                    node_id = await transport.handshake()
                except Exception as e:
                    self.logger.info(f'handshake failure from incoming connection: {e!r}')
                    return
                await self._add_peer_from_transport(node_id=node_id, transport=transport)
            try:
                self.listen_server = await asyncio.start_server(cb, addr, netaddr.port)
            except OSError as e:
                self.logger.error(f"cannot listen for lightning p2p. error: {e!r}")

    @ignore_exceptions  # don't kill outer taskgroup
    async def main_loop(self):
        self.logger.info("starting taskgroup.")
        try:
            async with self.taskgroup as group:
                await group.spawn(self._maintain_connectivity())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.exception("taskgroup died.")
        finally:
            self.logger.info("taskgroup stopped.")

    async def _maintain_connectivity(self):
        while True:
            await asyncio.sleep(1)
            if self.stopping_soon:
                return
            now = time.time()
            if len(self._peers) >= NUM_PEERS_TARGET:
                continue
            peers = await self._get_next_peers_to_try()
            for peer in peers:
                if self._can_retry_addr(peer, now=now):
                    try:
                        await self._add_peer(peer.host, peer.port, peer.pubkey)
                    except ErrorAddingPeer as e:
                        self.logger.info(f"failed to add peer: {peer}. exc: {e!r}")

    async def _add_peer(self, host: str, port: int, node_id: bytes) -> Peer:
        if node_id in self._peers:
            return self._peers[node_id]
        port = int(port)
        peer_addr = LNPeerAddr(host, port, node_id)
        self._trying_addr_now(peer_addr)
        self.logger.info(f"adding peer {peer_addr}")
        if node_id == self.node_keypair.pubkey:
            raise ErrorAddingPeer("cannot connect to self")
        transport = LNTransport(self.node_keypair.privkey, peer_addr,
                                proxy=self.network.proxy)
        peer = await self._add_peer_from_transport(node_id=node_id, transport=transport)
        return peer

    async def _add_peer_from_transport(self, *, node_id: bytes, transport: LNTransportBase) -> Peer:
        peer = Peer(self, node_id, transport)
        with self.lock:
            existing_peer = self._peers.get(node_id)
            if existing_peer:
                existing_peer.close_and_cleanup()
            assert node_id not in self._peers
            self._peers[node_id] = peer
        await self.taskgroup.spawn(peer.main_loop())
        return peer

    def peer_closed(self, peer: Peer) -> None:
        with self.lock:
            peer2 = self._peers.get(peer.pubkey)
            if peer2 is peer:
                self._peers.pop(peer.pubkey)

    def num_peers(self) -> int:
        return sum([p.is_initialized() for p in self.peers.values()])

    def start_network(self, network: 'Network'):
        assert network
        self.network = network
        self.config = network.config
        self._add_peers_from_config()
        asyncio.run_coroutine_threadsafe(self.main_loop(), self.network.asyncio_loop)

    async def stop(self):
        if self.listen_server:
            self.listen_server.close()
        util.unregister_callback(self.on_proxy_changed)
        await self.taskgroup.cancel_remaining()

    def _add_peers_from_config(self):
        peer_list = self.config.get('lightning_peers', [])
        for host, port, pubkey in peer_list:
            asyncio.run_coroutine_threadsafe(
                self._add_peer(host, int(port), bfh(pubkey)),
                self.network.asyncio_loop)

    def is_good_peer(self, peer: LNPeerAddr) -> bool:
        # the purpose of this method is to filter peers that advertise the desired feature bits
        # it is disabled for now, because feature bits published in node announcements seem to be unreliable
        return True
        node_id = peer.pubkey
        node = self.channel_db._nodes.get(node_id)
        if not node:
            return False
        try:
            ln_compare_features(self.features, node.features)
        except IncompatibleLightningFeatures:
            return False
        #self.logger.info(f'is_good {peer.host}')
        return True

    def on_peer_successfully_established(self, peer: Peer) -> None:
        if isinstance(peer.transport, LNTransport):
            peer_addr = peer.transport.peer_addr
            # reset connection attempt count
            self._on_connection_successfully_established(peer_addr)
            # add into channel db
            if self.channel_db:
                self.channel_db.add_recent_peer(peer_addr)
            # save network address into channels we might have with peer
            for chan in peer.channels.values():
                chan.add_or_update_peer_addr(peer_addr)

    async def _get_next_peers_to_try(self) -> Sequence[LNPeerAddr]:
        now = time.time()
        await self.channel_db.data_loaded.wait()
        # first try from recent peers
        recent_peers = self.channel_db.get_recent_peers()
        for peer in recent_peers:
            if not peer:
                continue
            if peer.pubkey in self._peers:
                continue
            if not self._can_retry_addr(peer, now=now):
                continue
            if not self.is_good_peer(peer):
                continue
            return [peer]
        # try random peer from graph
        unconnected_nodes = self.channel_db.get_200_randomly_sorted_nodes_not_in(self.peers.keys())
        if unconnected_nodes:
            for node_id in unconnected_nodes:
                addrs = self.channel_db.get_node_addresses(node_id)
                if not addrs:
                    continue
                host, port, timestamp = self.choose_preferred_address(list(addrs))
                try:
                    peer = LNPeerAddr(host, port, node_id)
                except ValueError:
                    continue
                if not self._can_retry_addr(peer, now=now):
                    continue
                if not self.is_good_peer(peer):
                    continue
                #self.logger.info('taking random ln peer from our channel db')
                return [peer]

        # getting desperate... let's try hardcoded fallback list of peers
        if constants.net in (constants.BitcoinTestnet,):
            fallback_list = FALLBACK_NODE_LIST_TESTNET
        elif constants.net in (constants.BitcoinMainnet,):
            fallback_list = FALLBACK_NODE_LIST_MAINNET
        else:
            return []  # regtest??

        fallback_list = [peer for peer in fallback_list if self._can_retry_addr(peer, now=now)]
        if fallback_list:
            return [random.choice(fallback_list)]

        # last resort: try dns seeds (BOLT-10)
        return await run_in_thread(self._get_peers_from_dns_seeds)

    def _get_peers_from_dns_seeds(self) -> Sequence[LNPeerAddr]:
        # NOTE: potentially long blocking call, do not run directly on asyncio event loop.
        # Return several peers to reduce the number of dns queries.
        if not constants.net.LN_DNS_SEEDS:
            return []
        dns_seed = random.choice(constants.net.LN_DNS_SEEDS)
        self.logger.info('asking dns seed "{}" for ln peers'.format(dns_seed))
        try:
            # note: this might block for several seconds
            # this will include bech32-encoded-pubkeys and ports
            srv_answers = resolve_dns_srv('r{}.{}'.format(
                constants.net.LN_REALM_BYTE, dns_seed))
        except dns.exception.DNSException as e:
            self.logger.info(f'failed querying (1) dns seed "{dns_seed}" for ln peers: {repr(e)}')
            return []
        random.shuffle(srv_answers)
        num_peers = 2 * NUM_PEERS_TARGET
        srv_answers = srv_answers[:num_peers]
        # we now have pubkeys and ports but host is still needed
        peers = []
        for srv_ans in srv_answers:
            try:
                # note: this might block for several seconds
                answers = dns.resolver.resolve(srv_ans['host'])
            except dns.exception.DNSException as e:
                self.logger.info(f'failed querying (2) dns seed "{dns_seed}" for ln peers: {repr(e)}')
                continue
            try:
                ln_host = str(answers[0])
                port = int(srv_ans['port'])
                bech32_pubkey = srv_ans['host'].split('.')[0]
                pubkey = get_compressed_pubkey_from_bech32(bech32_pubkey)
                peers.append(LNPeerAddr(ln_host, port, pubkey))
            except Exception as e:
                self.logger.info(f'error with parsing peer from dns seed: {repr(e)}')
                continue
        self.logger.info(f'got {len(peers)} ln peers from dns seed')
        return peers

    @staticmethod
    def choose_preferred_address(addr_list: Sequence[Tuple[str, int, int]]) -> Tuple[str, int, int]:
        assert len(addr_list) >= 1
        # choose first one that is an IP
        for host, port, timestamp in addr_list:
            if is_ip_address(host):
                return host, port, timestamp
        # otherwise choose one at random
        # TODO maybe filter out onion if not on tor?
        choice = random.choice(addr_list)
        return choice

    def on_proxy_changed(self, event, *args):
        for peer in self.peers.values():
            peer.close_and_cleanup()
        self._clear_addr_retry_times()

    @log_exceptions
    async def add_peer(self, connect_str: str) -> Peer:
        node_id, rest = extract_nodeid(connect_str)
        peer = self._peers.get(node_id)
        if not peer:
            if rest is not None:
                host, port = split_host_port(rest)
            else:
                if not self.channel_db:
                    addr = trampolines_by_id().get(node_id)
                    if not addr:
                        raise ConnStringFormatError(_('Address unknown for node:') + ' ' + bh2u(node_id))
                    host, port = addr.host, addr.port
                else:
                    addrs = self.channel_db.get_node_addresses(node_id)
                    if not addrs:
                        raise ConnStringFormatError(_('Don\'t know any addresses for node:') + ' ' + bh2u(node_id))
                    host, port, timestamp = self.choose_preferred_address(list(addrs))
            port = int(port)
            # Try DNS-resolving the host (if needed). This is simply so that
            # the caller gets a nice exception if it cannot be resolved.
            try:
                await asyncio.get_event_loop().getaddrinfo(host, port)
            except socket.gaierror:
                raise ConnStringFormatError(_('Hostname does not resolve (getaddrinfo failed)'))
            # add peer
            peer = await self._add_peer(host, port, node_id)
        return peer


class LNGossip(LNWorker):
    max_age = 14*24*3600
    LOGGING_SHORTCUT = 'g'

    def __init__(self):
        seed = os.urandom(32)
        node = BIP32Node.from_rootseed(seed, xtype='standard')
        xprv = node.to_xprv()
        super().__init__(xprv, LNGOSSIP_FEATURES)
        self.unknown_ids = set()

    def start_network(self, network: 'Network'):
        assert network
        super().start_network(network)
        asyncio.run_coroutine_threadsafe(self.taskgroup.spawn(self.maintain_db()), self.network.asyncio_loop)

    async def maintain_db(self):
        await self.channel_db.data_loaded.wait()
        while True:
            if len(self.unknown_ids) == 0:
                self.channel_db.prune_old_policies(self.max_age)
                self.channel_db.prune_orphaned_channels()
            await asyncio.sleep(120)

    async def add_new_ids(self, ids: Iterable[bytes]):
        known = self.channel_db.get_channel_ids()
        new = set(ids) - set(known)
        self.unknown_ids.update(new)
        util.trigger_callback('unknown_channels', len(self.unknown_ids))
        util.trigger_callback('gossip_peers', self.num_peers())
        util.trigger_callback('ln_gossip_sync_progress')

    def get_ids_to_query(self) -> Sequence[bytes]:
        N = 500
        l = list(self.unknown_ids)
        self.unknown_ids = set(l[N:])
        util.trigger_callback('unknown_channels', len(self.unknown_ids))
        util.trigger_callback('ln_gossip_sync_progress')
        return l[0:N]

    def get_sync_progress_estimate(self) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        """Estimates the gossip synchronization process and returns the number
        of synchronized channels, the total channels in the network and a
        rescaled percentage of the synchronization process."""
        if self.num_peers() == 0:
            return None, None, None
        nchans_with_0p, nchans_with_1p, nchans_with_2p = self.channel_db.get_num_channels_partitioned_by_policy_count()
        num_db_channels = nchans_with_0p + nchans_with_1p + nchans_with_2p
        # some channels will never have two policies (only one is in gossip?...)
        # so if we have at least 1 policy for a channel, we consider that channel "complete" here
        current_est = num_db_channels - nchans_with_0p
        total_est = len(self.unknown_ids) + num_db_channels

        progress = current_est / total_est if total_est and current_est else 0
        progress_percent = (1.0 / 0.95 * progress) * 100
        progress_percent = min(progress_percent, 100)
        progress_percent = round(progress_percent)
        # take a minimal number of synchronized channels to get a more accurate
        # percentage estimate
        if current_est < 200:
            progress_percent = 0
        return current_est, total_est, progress_percent

    async def process_gossip(self, chan_anns, node_anns, chan_upds):
        # note: we run in the originating peer's TaskGroup, so we can safely raise here
        #       and disconnect only from that peer
        await self.channel_db.data_loaded.wait()
        self.logger.debug(f'process_gossip {len(chan_anns)} {len(node_anns)} {len(chan_upds)}')
        # channel announcements
        def process_chan_anns():
            for payload in chan_anns:
                self.channel_db.verify_channel_announcement(payload)
            self.channel_db.add_channel_announcements(chan_anns)
        await run_in_thread(process_chan_anns)
        # node announcements
        def process_node_anns():
            for payload in node_anns:
                self.channel_db.verify_node_announcement(payload)
            self.channel_db.add_node_announcements(node_anns)
        await run_in_thread(process_node_anns)
        # channel updates
        categorized_chan_upds = await run_in_thread(partial(
            self.channel_db.add_channel_updates,
            chan_upds,
            max_age=self.max_age))
        orphaned = categorized_chan_upds.orphaned
        if orphaned:
            self.logger.info(f'adding {len(orphaned)} unknown channel ids')
            orphaned_ids = [c['short_channel_id'] for c in orphaned]
            await self.add_new_ids(orphaned_ids)
        if categorized_chan_upds.good:
            self.logger.debug(f'on_channel_update: {len(categorized_chan_upds.good)}/{len(chan_upds)}')


class LNWallet(LNWorker):

    lnwatcher: Optional['LNWalletWatcher']
    MPP_EXPIRY = 120
    TIMEOUT_SHUTDOWN_FAIL_PENDING_HTLCS = 3  # seconds

    def __init__(self, wallet: 'Abstract_Wallet', xprv):
        self.wallet = wallet
        self.db = wallet.db
        Logger.__init__(self)
        LNWorker.__init__(self, xprv, LNWALLET_FEATURES)
        self.config = wallet.config
        self.lnwatcher = None
        self.lnrater: LNRater = None
        self.payments = self.db.get_dict('lightning_payments')     # RHASH -> amount, direction, is_paid
        self.preimages = self.db.get_dict('lightning_preimages')   # RHASH -> preimage
        # note: this sweep_address is only used as fallback; as it might result in address-reuse
        self.sweep_address = wallet.get_new_sweep_address_for_channel()
        self.logs = defaultdict(list)  # type: Dict[str, List[HtlcLog]]  # key is RHASH  # (not persisted)
        # used in tests
        self.enable_htlc_settle = True
        self.enable_htlc_forwarding = True

        # note: accessing channels (besides simple lookup) needs self.lock!
        self._channels = {}  # type: Dict[bytes, Channel]
        channels = self.db.get_dict("channels")
        for channel_id, c in random_shuffled_copy(channels.items()):
            self._channels[bfh(channel_id)] = Channel(c, sweep_address=self.sweep_address, lnworker=self)

        self._channel_backups = {}  # type: Dict[bytes, ChannelBackup]
        # order is important: imported should overwrite onchain
        for name in ["onchain_channel_backups", "imported_channel_backups"]:
            channel_backups = self.db.get_dict(name)
            for channel_id, storage in channel_backups.items():
                self._channel_backups[bfh(channel_id)] = ChannelBackup(storage, sweep_address=self.sweep_address, lnworker=self)

        self.sent_htlcs = defaultdict(asyncio.Queue)  # type: Dict[bytes, asyncio.Queue[HtlcLog]]
        self.sent_htlcs_routes = dict()               # (RHASH, scid, htlc_id) -> route, payment_secret, amount_msat, bucket_msat
        self.sent_buckets = dict()                    # payment_secret -> (amount_sent, amount_failed)
        self.received_mpp_htlcs = dict()                  # RHASH -> mpp_status, htlc_set

        self.swap_manager = SwapManager(wallet=self.wallet, lnworker=self)
        # detect inflight payments
        self.inflight_payments = set()        # (not persisted) keys of invoices that are in PR_INFLIGHT state
        for payment_hash in self.get_payments(status='inflight').keys():
            self.set_invoice_status(payment_hash.hex(), PR_INFLIGHT)

        self.trampoline_forwarding_failures = {} # todo: should be persisted

    def has_deterministic_node_id(self):
        return bool(self.db.get('lightning_xprv'))

    def has_recoverable_channels(self):
        # TODO: expose use_recoverable_channels in preferences
        return self.has_deterministic_node_id() \
            and self.config.get('use_recoverable_channels', True) \
            and not (self.config.get('lightning_listen'))

    @property
    def channels(self) -> Mapping[bytes, Channel]:
        """Returns a read-only copy of channels."""
        with self.lock:
            return self._channels.copy()

    @property
    def channel_backups(self) -> Mapping[bytes, ChannelBackup]:
        """Returns a read-only copy of channels."""
        with self.lock:
            return self._channel_backups.copy()

    def get_channel_by_id(self, channel_id: bytes) -> Optional[Channel]:
        return self._channels.get(channel_id, None)

    def diagnostic_name(self):
        return self.wallet.diagnostic_name()

    @ignore_exceptions
    @log_exceptions
    async def sync_with_local_watchtower(self):
        watchtower = self.network.local_watchtower
        if watchtower:
            while True:
                for chan in self.channels.values():
                    await self.sync_channel_with_watchtower(chan, watchtower.sweepstore)
                await asyncio.sleep(5)

    @ignore_exceptions
    @log_exceptions
    async def sync_with_remote_watchtower(self):
        while True:
            # periodically poll if the user updated 'watchtower_url'
            await asyncio.sleep(5)
            watchtower_url = self.config.get('watchtower_url')
            if not watchtower_url:
                continue
            parsed_url = urllib.parse.urlparse(watchtower_url)
            if not (parsed_url.scheme == 'https' or is_private_netaddress(parsed_url.hostname)):
                self.logger.warning(f"got watchtower URL for remote tower but we won't use it! "
                                    f"can only use HTTPS (except if private IP): not using {watchtower_url!r}")
                continue
            # try to sync with the remote watchtower
            try:
                async with make_aiohttp_session(proxy=self.network.proxy) as session:
                    watchtower = JsonRPCClient(session, watchtower_url)
                    watchtower.add_method('get_ctn')
                    watchtower.add_method('add_sweep_tx')
                    for chan in self.channels.values():
                        await self.sync_channel_with_watchtower(chan, watchtower)
            except aiohttp.client_exceptions.ClientConnectorError:
                self.logger.info(f'could not contact remote watchtower {watchtower_url}')

    async def sync_channel_with_watchtower(self, chan: Channel, watchtower):
        outpoint = chan.funding_outpoint.to_str()
        addr = chan.get_funding_address()
        current_ctn = chan.get_oldest_unrevoked_ctn(REMOTE)
        watchtower_ctn = await watchtower.get_ctn(outpoint, addr)
        for ctn in range(watchtower_ctn + 1, current_ctn):
            sweeptxs = chan.create_sweeptxs(ctn)
            for tx in sweeptxs:
                await watchtower.add_sweep_tx(outpoint, ctn, tx.inputs()[0].prevout.to_str(), tx.serialize())

    def start_network(self, network: 'Network'):
        assert network
        self.network = network
        self.config = network.config
        self.lnwatcher = LNWalletWatcher(self, network)
        self.lnwatcher.start_network(network)
        self.swap_manager.start_network(network=network, lnwatcher=self.lnwatcher)
        self.lnrater = LNRater(self, network)

        for chan in self.channels.values():
            self.lnwatcher.add_channel(chan.funding_outpoint.to_str(), chan.get_funding_address())
        for cb in self.channel_backups.values():
            self.lnwatcher.add_channel(cb.funding_outpoint.to_str(), cb.get_funding_address())

        for coro in [
                self.maybe_listen(),
                self.lnwatcher.on_network_update('network_updated'), # shortcut (don't block) if funding tx locked and verified
                self.reestablish_peers_and_channels(),
                self.sync_with_local_watchtower(),
                self.sync_with_remote_watchtower(),
        ]:
            tg_coro = self.taskgroup.spawn(coro)
            asyncio.run_coroutine_threadsafe(tg_coro, self.network.asyncio_loop)

    async def stop(self):
        self.stopping_soon = True
        if self.listen_server:  # stop accepting new peers
            self.listen_server.close()
        async with ignore_after(self.TIMEOUT_SHUTDOWN_FAIL_PENDING_HTLCS):
            await self.wait_for_received_pending_htlcs_to_get_removed()
        await LNWorker.stop(self)
        if self.lnwatcher:
            await self.lnwatcher.stop()
            self.lnwatcher = None

    async def wait_for_received_pending_htlcs_to_get_removed(self):
        assert self.stopping_soon is True
        # We try to fail pending MPP HTLCs, and wait a bit for them to get removed.
        # Note: even without MPP, if we just failed/fulfilled an HTLC, it is good
        #       to wait a bit for it to become irrevocably removed.
        # Note: we don't wait for *all htlcs* to get removed, only for those
        #       that we can already fail/fulfill. e.g. forwarded htlcs cannot be removed
        async with TaskGroup() as group:
            for peer in self.peers.values():
                await group.spawn(peer.wait_one_htlc_switch_iteration())
        while True:
            if all(not peer.received_htlcs_pending_removal for peer in self.peers.values()):
                break
            async with TaskGroup(wait=any) as group:
                for peer in self.peers.values():
                    await group.spawn(peer.received_htlc_removed_event.wait())

    def peer_closed(self, peer):
        for chan in self.channels_for_peer(peer.pubkey).values():
            chan.peer_state = PeerState.DISCONNECTED
            util.trigger_callback('channel', self.wallet, chan)
        super().peer_closed(peer)

    def get_payments(self, *, status=None):
        # return one item per payment_hash
        # note: with AMP we will have several channels per payment
        out = defaultdict(list)
        for chan in self.channels.values():
            d = chan.get_payments(status=status)
            for k, v in d.items():
                out[k] += v
        return out

    def get_payment_value(self, info: Optional['PaymentInfo'], plist):
        amount_msat = 0
        fee_msat = None
        for chan_id, htlc, _direction, _status in plist:
            amount_msat += int(_direction) * htlc.amount_msat
            if _direction == SENT and info and info.amount_msat:
                fee_msat = (fee_msat or 0) - info.amount_msat - amount_msat
        timestamp = min([htlc.timestamp for chan_id, htlc, _direction, _status in plist])
        return amount_msat, fee_msat, timestamp

    def get_lightning_history(self):
        out = {}
        for payment_hash, plist in self.get_payments(status='settled').items():
            if len(plist) == 0:
                continue
            key = payment_hash.hex()
            info = self.get_payment_info(payment_hash)
            amount_msat, fee_msat, timestamp = self.get_payment_value(info, plist)
            if info is not None:
                label = self.wallet.get_label(key)
                direction = ('sent' if info.direction == SENT else 'received') if len(plist)==1 else 'self-payment'
            else:
                direction = 'forwarding'
                label = _('Forwarding')
            preimage = self.get_preimage(payment_hash).hex()
            item = {
                'type': 'payment',
                'label': label,
                'timestamp': timestamp or 0,
                'date': timestamp_to_datetime(timestamp),
                'direction': direction,
                'amount_msat': amount_msat,
                'fee_msat': fee_msat,
                'payment_hash': key,
                'preimage': preimage,
            }
            # add group_id to swap transactions
            swap = self.swap_manager.get_swap(payment_hash)
            if swap:
                if swap.is_reverse:
                    item['group_id'] = swap.spending_txid
                    item['group_label'] = 'Reverse swap' + ' ' + self.config.format_amount_and_units(swap.lightning_amount)
                else:
                    item['group_id'] = swap.funding_txid
                    item['group_label'] = 'Forward swap' + ' ' + self.config.format_amount_and_units(swap.onchain_amount)
            # done
            out[payment_hash] = item
        return out

    def get_onchain_history(self):
        current_height = self.wallet.get_local_height()
        out = {}
        # add funding events
        for chan in self.channels.values():
            item = chan.get_funding_height()
            if item is None:
                continue
            funding_txid, funding_height, funding_timestamp = item
            tx_height = self.lnwatcher.get_tx_height(funding_txid)
            item = {
                'channel_id': bh2u(chan.channel_id),
                'type': 'channel_opening',
                'label': self.wallet.get_label_for_txid(funding_txid) or (_('Open channel') + ' ' + chan.get_id_for_log()),
                'txid': funding_txid,
                'amount_msat': chan.balance(LOCAL, ctn=0),
                'direction': 'received',
                'timestamp': tx_height.timestamp,
                'date': timestamp_to_datetime(tx_height.timestamp),
                'fee_sat': None,
                'fee_msat': None,
                'height': tx_height.height,
                'confirmations': tx_height.conf,
            }
            out[funding_txid] = item
            item = chan.get_closing_height()
            if item is None:
                continue
            closing_txid, closing_height, closing_timestamp = item
            tx_height = self.lnwatcher.get_tx_height(closing_txid)
            item = {
                'channel_id': bh2u(chan.channel_id),
                'txid': closing_txid,
                'label': self.wallet.get_label_for_txid(closing_txid) or (_('Close channel') + ' ' + chan.get_id_for_log()),
                'type': 'channel_closure',
                'amount_msat': -chan.balance_minus_outgoing_htlcs(LOCAL),
                'direction': 'sent',
                'timestamp': tx_height.timestamp,
                'date': timestamp_to_datetime(tx_height.timestamp),
                'fee_sat': None,
                'fee_msat': None,
                'height': tx_height.height,
                'confirmations': tx_height.conf,
            }
            out[closing_txid] = item
        # add info about submarine swaps
        settled_payments = self.get_payments(status='settled')
        for payment_hash_hex, swap in self.swap_manager.swaps.items():
            txid = swap.spending_txid if swap.is_reverse else swap.funding_txid
            if txid is None:
                continue
            payment_hash = bytes.fromhex(payment_hash_hex)
            if payment_hash in settled_payments:
                plist = settled_payments[payment_hash]
                info = self.get_payment_info(payment_hash)
                amount_msat, fee_msat, timestamp = self.get_payment_value(info, plist)
            else:
                amount_msat = 0
            label = 'Reverse swap' if swap.is_reverse else 'Forward swap'
            delta = current_height - swap.locktime
            if not swap.is_redeemed and swap.spending_txid is None and delta < 0:
                label += f' (refundable in {-delta} blocks)' # fixme: only if unspent
            out[txid] = {
                'txid': txid,
                'group_id': txid,
                'amount_msat': 0,
                #'amount_msat': amount_msat, # must not be added
                'type': 'swap',
                'label': self.wallet.get_label_for_txid(txid) or label,
            }
        return out

    def get_history(self):
        out = list(self.get_lightning_history().values()) + list(self.get_onchain_history().values())
        # sort by timestamp
        out.sort(key=lambda x: (x.get('timestamp') or float("inf")))
        balance_msat = 0
        for item in out:
            balance_msat += item['amount_msat']
            item['balance_msat'] = balance_msat
        return out

    def channel_peers(self) -> List[bytes]:
        node_ids = [chan.node_id for chan in self.channels.values() if not chan.is_closed()]
        return node_ids

    def channels_for_peer(self, node_id):
        assert type(node_id) is bytes
        return {chan_id: chan for (chan_id, chan) in self.channels.items()
                if chan.node_id == node_id}

    def channel_state_changed(self, chan: Channel):
        if type(chan) is Channel:
            self.save_channel(chan)
        util.trigger_callback('channel', self.wallet, chan)

    def save_channel(self, chan: Channel):
        assert type(chan) is Channel
        if chan.config[REMOTE].next_per_commitment_point == chan.config[REMOTE].current_per_commitment_point:
            raise Exception("Tried to save channel with next_point == current_point, this should not happen")
        self.wallet.save_db()
        util.trigger_callback('channel', self.wallet, chan)

    def channel_by_txo(self, txo: str) -> Optional[AbstractChannel]:
        for chan in self.channels.values():
            if chan.funding_outpoint.to_str() == txo:
                return chan
        for chan in self.channel_backups.values():
            if chan.funding_outpoint.to_str() == txo:
                return chan

    async def on_channel_update(self, chan: Channel):
        if type(chan) is ChannelBackup:
            util.trigger_callback('channel', self.wallet, chan)
            return

        if chan.get_state() == ChannelState.OPEN and chan.should_be_closed_due_to_expiring_htlcs(self.network.get_local_height()):
            self.logger.info(f"force-closing due to expiring htlcs")
            await self.try_force_closing(chan.channel_id)

        elif chan.get_state() == ChannelState.FUNDED:
            peer = self._peers.get(chan.node_id)
            if peer and peer.is_initialized():
                peer.send_funding_locked(chan)

        elif chan.get_state() == ChannelState.OPEN:
            peer = self._peers.get(chan.node_id)
            if peer:
                await peer.maybe_update_fee(chan)
                conf = self.lnwatcher.get_tx_height(chan.funding_outpoint.txid).conf
                peer.on_network_update(chan, conf)

        elif chan.get_state() == ChannelState.FORCE_CLOSING:
            force_close_tx = chan.force_close_tx()
            txid = force_close_tx.txid()
            height = self.lnwatcher.get_tx_height(txid).height
            if height == TX_HEIGHT_LOCAL:
                self.logger.info('REBROADCASTING CLOSING TX')
                await self.network.try_broadcasting(force_close_tx, 'force-close')

    @log_exceptions
    async def _open_channel_coroutine(
            self, *,
            connect_str: str,
            funding_tx: PartialTransaction,
            funding_sat: int,
            push_sat: int,
            password: Optional[str]) -> Tuple[Channel, PartialTransaction]:

        peer = await self.add_peer(connect_str)
        coro = peer.channel_establishment_flow(
            funding_tx=funding_tx,
            funding_sat=funding_sat,
            push_msat=push_sat * 1000,
            temp_channel_id=os.urandom(32))
        chan, funding_tx = await asyncio.wait_for(coro, LN_P2P_NETWORK_TIMEOUT)
        util.trigger_callback('channels_updated', self.wallet)
        self.wallet.add_transaction(funding_tx)  # save tx as local into the wallet
        self.wallet.sign_transaction(funding_tx, password)
        self.wallet.set_label(funding_tx.txid(), _('Open channel'))
        if funding_tx.is_complete():
            await self.network.try_broadcasting(funding_tx, 'open_channel')
        return chan, funding_tx

    def add_channel(self, chan: Channel):
        with self.lock:
            self._channels[chan.channel_id] = chan
        self.lnwatcher.add_channel(chan.funding_outpoint.to_str(), chan.get_funding_address())

    def add_new_channel(self, chan: Channel):
        self.add_channel(chan)
        channels_db = self.db.get_dict('channels')
        channels_db[chan.channel_id.hex()] = chan.storage
        for addr in chan.get_wallet_addresses_channel_might_want_reserved():
            self.wallet.set_reserved_state_of_address(addr, reserved=True)
        try:
            self.save_channel(chan)
            backup_dir = self.config.get_backup_dir()
            if backup_dir is not None:
                self.wallet.save_backup(backup_dir)
        except:
            chan.set_state(ChannelState.REDEEMED)
            self.remove_channel(chan.channel_id)
            raise

    def cb_data(self, node_id):
        return CB_MAGIC_BYTES + node_id[0:16]

    def decrypt_cb_data(self, encrypted_data, funding_address):
        funding_scripthash = bytes.fromhex(address_to_scripthash(funding_address))
        nonce = funding_scripthash[0:12]
        return chacha20_decrypt(key=self.backup_key, data=encrypted_data, nonce=nonce)

    def encrypt_cb_data(self, data, funding_address):
        funding_scripthash = bytes.fromhex(address_to_scripthash(funding_address))
        nonce = funding_scripthash[0:12]
        return chacha20_encrypt(key=self.backup_key, data=data, nonce=nonce)

    def mktx_for_open_channel(
            self, *,
            coins: Sequence[PartialTxInput],
            funding_sat: int,
            node_id: bytes,
            fee_est=None) -> PartialTransaction:
        outputs = [PartialTxOutput.from_address_and_value(ln_dummy_address(), funding_sat)]
        if self.has_recoverable_channels():
            dummy_scriptpubkey = make_op_return(self.cb_data(node_id))
            outputs.append(PartialTxOutput(scriptpubkey=dummy_scriptpubkey, value=0))
        tx = self.wallet.make_unsigned_transaction(
            coins=coins,
            outputs=outputs,
            fee=fee_est)
        tx.set_rbf(False)
        return tx

    def open_channel(self, *, connect_str: str, funding_tx: PartialTransaction,
                     funding_sat: int, push_amt_sat: int, password: str = None) -> Tuple[Channel, PartialTransaction]:
        if funding_sat > LN_MAX_FUNDING_SAT:
            raise Exception(_("Requested channel capacity is over protocol allowed maximum."))
        coro = self._open_channel_coroutine(
            connect_str=connect_str, funding_tx=funding_tx, funding_sat=funding_sat,
            push_sat=push_amt_sat, password=password)
        fut = asyncio.run_coroutine_threadsafe(coro, self.network.asyncio_loop)
        try:
            chan, funding_tx = fut.result()
        except concurrent.futures.TimeoutError:
            raise Exception(_("open_channel timed out"))
        return chan, funding_tx

    def get_channel_by_short_id(self, short_channel_id: bytes) -> Optional[Channel]:
        for chan in self.channels.values():
            if chan.short_channel_id == short_channel_id:
                return chan

    def create_routes_from_invoice(self, amount_msat: int, decoded_invoice: LnAddr, *, full_path=None):
        return self.create_routes_for_payment(
            amount_msat=amount_msat,
            final_total_msat=amount_msat,
            invoice_pubkey=decoded_invoice.pubkey.serialize(),
            min_cltv_expiry=decoded_invoice.get_min_final_cltv_expiry(),
            r_tags=decoded_invoice.get_routing_info('r'),
            invoice_features=decoded_invoice.get_features(),
            trampoline_fee_level=0,
            use_two_trampolines=False,
            payment_hash=decoded_invoice.paymenthash,
            payment_secret=decoded_invoice.payment_secret,
            full_path=full_path)

    @log_exceptions
    async def pay_invoice(
            self, invoice: str, *,
            amount_msat: int = None,
            attempts: int = 1,
            full_path: LNPaymentPath = None) -> Tuple[bool, List[HtlcLog]]:

        lnaddr = self._check_invoice(invoice, amount_msat=amount_msat)
        min_cltv_expiry = lnaddr.get_min_final_cltv_expiry()
        payment_hash = lnaddr.paymenthash
        key = payment_hash.hex()
        payment_secret = lnaddr.payment_secret
        invoice_pubkey = lnaddr.pubkey.serialize()
        invoice_features = lnaddr.get_features()
        r_tags = lnaddr.get_routing_info('r')
        amount_to_pay = lnaddr.get_amount_msat()
        status = self.get_payment_status(payment_hash)
        if status == PR_PAID:
            raise PaymentFailure(_("This invoice has been paid already"))
        if status == PR_INFLIGHT:
            raise PaymentFailure(_("A payment was already initiated for this invoice"))
        if payment_hash in self.get_payments(status='inflight'):
            raise PaymentFailure(_("A previous attempt to pay this invoice did not clear"))
        info = PaymentInfo(payment_hash, amount_to_pay, SENT, PR_UNPAID)
        self.save_payment_info(info)
        self.wallet.set_label(key, lnaddr.get_description())

        self.set_invoice_status(key, PR_INFLIGHT)
        try:
            await self.pay_to_node(
                node_pubkey=invoice_pubkey,
                payment_hash=payment_hash,
                payment_secret=payment_secret,
                amount_to_pay=amount_to_pay,
                min_cltv_expiry=min_cltv_expiry,
                r_tags=r_tags,
                invoice_features=invoice_features,
                attempts=attempts,
                full_path=full_path)
            success = True
        except PaymentFailure as e:
            self.logger.info(f'payment failure: {e!r}')
            success = False
            reason = str(e)
        if success:
            self.set_invoice_status(key, PR_PAID)
            util.trigger_callback('payment_succeeded', self.wallet, key)
        else:
            self.set_invoice_status(key, PR_UNPAID)
            util.trigger_callback('payment_failed', self.wallet, key, reason)
        log = self.logs[key]
        return success, log

    async def pay_to_node(
            self, *,
            node_pubkey: bytes,
            payment_hash: bytes,
            payment_secret: Optional[bytes],
            amount_to_pay: int,  # in msat
            min_cltv_expiry: int,
            r_tags,
            invoice_features: int,
            attempts: int = 1,
            full_path: LNPaymentPath = None,
            fwd_trampoline_onion=None,
            fwd_trampoline_fee=None,
            fwd_trampoline_cltv_delta=None) -> None:

        if fwd_trampoline_onion:
            # todo: compare to the fee of the actual route we found
            if fwd_trampoline_fee < 1000:
                raise OnionRoutingFailure(code=OnionFailureCode.TRAMPOLINE_FEE_INSUFFICIENT, data=b'')
            if fwd_trampoline_cltv_delta < 576:
                raise OnionRoutingFailure(code=OnionFailureCode.TRAMPOLINE_EXPIRY_TOO_SOON, data=b'')

        self.logs[payment_hash.hex()] = log = []
        trampoline_fee_level = self.INITIAL_TRAMPOLINE_FEE_LEVEL
        use_two_trampolines = True # only used for pay to legacy

        amount_inflight = 0  # what we sent in htlcs (that receiver gets, without fees)
        while True:
            amount_to_send = amount_to_pay - amount_inflight
            if amount_to_send > 0:
                # 1. create a set of routes for remaining amount.
                # note: path-finding runs in a separate thread so that we don't block the asyncio loop
                # graph updates might occur during the computation
                routes = await run_in_thread(partial(
                    self.create_routes_for_payment,
                    amount_msat=amount_to_send,
                    final_total_msat=amount_to_pay,
                    invoice_pubkey=node_pubkey,
                    min_cltv_expiry=min_cltv_expiry,
                    r_tags=r_tags,
                    invoice_features=invoice_features,
                    full_path=full_path,
                    payment_hash=payment_hash,
                    payment_secret=payment_secret,
                    trampoline_fee_level=trampoline_fee_level,
                    use_two_trampolines=use_two_trampolines,
                    fwd_trampoline_onion=fwd_trampoline_onion))
                # 2. send htlcs
                for route, amount_msat, total_msat, amount_receiver_msat, cltv_delta, bucket_payment_secret, trampoline_onion in routes:
                    amount_inflight += amount_receiver_msat
                    if amount_inflight > amount_to_pay:  # safety belts
                        raise Exception(f"amount_inflight={amount_inflight} > amount_to_pay={amount_to_pay}")
                    await self.pay_to_route(
                        route=route,
                        amount_msat=amount_msat,
                        total_msat=total_msat,
                        amount_receiver_msat=amount_receiver_msat,
                        payment_hash=payment_hash,
                        payment_secret=bucket_payment_secret,
                        min_cltv_expiry=cltv_delta,
                        trampoline_onion=trampoline_onion)
                util.trigger_callback('invoice_status', self.wallet, payment_hash.hex())
            # 3. await a queue
            self.logger.info(f"amount inflight {amount_inflight}")
            htlc_log = await self.sent_htlcs[payment_hash].get()
            amount_inflight -= htlc_log.amount_msat
            if amount_inflight < 0:
                raise Exception(f"amount_inflight={amount_inflight} < 0")
            log.append(htlc_log)
            if htlc_log.success:
                return
            # htlc failed
            if len(log) >= attempts:
                raise PaymentFailure('Giving up after %d attempts'%len(log))
            # if we get a tmp channel failure, it might work to split the amount and try more routes
            # if we get a channel update, we might retry the same route and amount
            route = htlc_log.route
            sender_idx = htlc_log.sender_idx
            failure_msg = htlc_log.failure_msg
            code, data = failure_msg.code, failure_msg.data
            self.logger.info(f"UPDATE_FAIL_HTLC. code={repr(code)}. "
                             f"decoded_data={failure_msg.decode_data()}. data={data.hex()!r}")
            self.logger.info(f"error reported by {bh2u(route[sender_idx].node_id)}")
            if code == OnionFailureCode.MPP_TIMEOUT:
                raise PaymentFailure(failure_msg.code_name())
            # trampoline
            if not self.channel_db:
                if code == OnionFailureCode.TRAMPOLINE_FEE_INSUFFICIENT:
                    # todo: parse the node parameters here (not returned by eclair yet)
                    trampoline_fee_level += 1
                    continue
                elif use_two_trampolines:
                    use_two_trampolines = False
                else:
                    raise PaymentFailure(failure_msg.code_name())
            else:
                self.handle_error_code_from_failed_htlc(
                    route=route, sender_idx=sender_idx, failure_msg=failure_msg)

    async def pay_to_route(
            self, *,
            route: LNPaymentRoute,
            amount_msat: int,
            total_msat: int,
            amount_receiver_msat:int,
            payment_hash: bytes,
            payment_secret: Optional[bytes],
            min_cltv_expiry: int,
            trampoline_onion: bytes = None) -> None:

        # send a single htlc
        short_channel_id = route[0].short_channel_id
        chan = self.get_channel_by_short_id(short_channel_id)
        peer = self._peers.get(route[0].node_id)
        if not peer:
            raise PaymentFailure('Dropped peer')
        await peer.initialized
        htlc = peer.pay(
            route=route,
            chan=chan,
            amount_msat=amount_msat,
            total_msat=total_msat,
            payment_hash=payment_hash,
            min_final_cltv_expiry=min_cltv_expiry,
            payment_secret=payment_secret,
            trampoline_onion=trampoline_onion)

        key = (payment_hash, short_channel_id, htlc.htlc_id)
        self.sent_htlcs_routes[key] = route, payment_secret, amount_msat, total_msat, amount_receiver_msat
        # if we sent MPP to a trampoline, add item to sent_buckets
        if not self.channel_db and amount_msat != total_msat:
            if payment_secret not in self.sent_buckets:
                self.sent_buckets[payment_secret] = (0, 0)
            amount_sent, amount_failed = self.sent_buckets[payment_secret]
            amount_sent += amount_receiver_msat
            self.sent_buckets[payment_secret] = amount_sent, amount_failed
        util.trigger_callback('htlc_added', chan, htlc, SENT)

    def handle_error_code_from_failed_htlc(
            self,
            *,
            route: LNPaymentRoute,
            sender_idx: int,
            failure_msg: OnionRoutingFailure) -> None:
        code, data = failure_msg.code, failure_msg.data
        # TODO can we use lnmsg.OnionWireSerializer here?
        # TODO update onion_wire.csv
        # handle some specific error codes
        failure_codes = {
            OnionFailureCode.TEMPORARY_CHANNEL_FAILURE: 0,
            OnionFailureCode.AMOUNT_BELOW_MINIMUM: 8,
            OnionFailureCode.FEE_INSUFFICIENT: 8,
            OnionFailureCode.INCORRECT_CLTV_EXPIRY: 4,
            OnionFailureCode.EXPIRY_TOO_SOON: 0,
            OnionFailureCode.CHANNEL_DISABLED: 2,
        }
        blacklist = False
        update = False
        if code in failure_codes:
            offset = failure_codes[code]
            channel_update_len = int.from_bytes(data[offset:offset+2], byteorder="big")
            channel_update_as_received = data[offset+2: offset+2+channel_update_len]
            payload = self._decode_channel_update_msg(channel_update_as_received)
            if payload is None:
                self.logger.info(f'could not decode channel_update for failed htlc: {channel_update_as_received.hex()}')
                blacklist = True
            else:
                blacklist, update = self._handle_chanupd_from_failed_htlc(
                    payload, route=route, sender_idx=sender_idx)
        else:
            blacklist = True

        if blacklist:
            # blacklist channel after reporter node
            # TODO this should depend on the error (even more granularity)
            # also, we need finer blacklisting (directed edges; nodes)
            if sender_idx is None:
                raise PaymentFailure(failure_msg.code_name())
            try:
                short_chan_id = route[sender_idx + 1].short_channel_id
            except IndexError:
                raise PaymentFailure(f'payment destination reported error: {failure_msg.code_name()}') from None
            # TODO: for MPP we need to save the amount for which
            # we saw temporary channel failure
            self.logger.info(f'blacklisting channel {short_chan_id}')
            self.network.channel_blacklist.add(short_chan_id)

        # we should not continue if we did not blacklist or update anything
        if not (blacklist or update):
            raise PaymentFailure(failure_msg.code_name())

    def _handle_chanupd_from_failed_htlc(self, payload, *, route, sender_idx) -> Tuple[bool, bool]:
        blacklist = False
        update = False
        try:
            r = self.channel_db.add_channel_update(payload, verify=True)
        except InvalidGossipMsg:
            return True, False  # blacklist
        short_channel_id = ShortChannelID(payload['short_channel_id'])
        if r == UpdateStatus.GOOD:
            self.logger.info(f"applied channel update to {short_channel_id}")
            # TODO: add test for this
            # FIXME: this does not work for our own unannounced channels.
            for chan in self.channels.values():
                if chan.short_channel_id == short_channel_id:
                    chan.set_remote_update(payload)
            update = True
        elif r == UpdateStatus.ORPHANED:
            # maybe it is a private channel (and data in invoice was outdated)
            self.logger.info(f"Could not find {short_channel_id}. maybe update is for private channel?")
            start_node_id = route[sender_idx].node_id
            update = self.channel_db.add_channel_update_for_private_channel(payload, start_node_id)
            blacklist = not update
        elif r == UpdateStatus.EXPIRED:
            blacklist = True
        elif r == UpdateStatus.DEPRECATED:
            self.logger.info(f'channel update is not more recent.')
            blacklist = True
        elif r == UpdateStatus.UNCHANGED:
            blacklist = True
        return blacklist, update

    @classmethod
    def _decode_channel_update_msg(cls, chan_upd_msg: bytes) -> Optional[Dict[str, Any]]:
        channel_update_as_received = chan_upd_msg
        channel_update_typed = (258).to_bytes(length=2, byteorder="big") + channel_update_as_received
        # note: some nodes put channel updates in error msgs with the leading msg_type already there.
        #       we try decoding both ways here.
        try:
            message_type, payload = decode_msg(channel_update_typed)
            if payload['chain_hash'] != constants.net.rev_genesis_bytes(): raise Exception()
            payload['raw'] = channel_update_typed
            return payload
        except:  # FIXME: too broad
            try:
                message_type, payload = decode_msg(channel_update_as_received)
                if payload['chain_hash'] != constants.net.rev_genesis_bytes(): raise Exception()
                payload['raw'] = channel_update_as_received
                return payload
            except:
                return None

    @staticmethod
    def _check_invoice(invoice: str, *, amount_msat: int = None) -> LnAddr:
        addr = lndecode(invoice, expected_hrp=constants.net.SEGWIT_HRP)
        if addr.is_expired():
            raise InvoiceError(_("This invoice has expired"))
        if amount_msat:  # replace amt in invoice. main usecase is paying zero amt invoices
            existing_amt_msat = addr.get_amount_msat()
            if existing_amt_msat and amount_msat < existing_amt_msat:
                raise Exception("cannot pay lower amt than what is originally in LN invoice")
            addr.amount = Decimal(amount_msat) / COIN / 1000
        if addr.amount is None:
            raise InvoiceError(_("Missing amount"))
        if addr.get_min_final_cltv_expiry() > lnutil.NBLOCK_CLTV_EXPIRY_TOO_FAR_INTO_FUTURE:
            raise InvoiceError("{}\n{}".format(
                _("Invoice wants us to risk locking funds for unreasonably long."),
                f"min_final_cltv_expiry: {addr.get_min_final_cltv_expiry()}"))
        return addr

    def is_trampoline_peer(self, node_id: bytes) -> bool:
        # until trampoline is advertised in lnfeatures, check against hardcoded list
        if is_hardcoded_trampoline(node_id):
            return True
        peer = self._peers.get(node_id)
        if peer and peer.their_features.supports(LnFeatures.OPTION_TRAMPOLINE_ROUTING_OPT):
            return True
        return False

    def suggest_peer(self) -> Optional[bytes]:
        if self.channel_db:
            return self.lnrater.suggest_peer()
        else:
            return random.choice(list(hardcoded_trampoline_nodes().values())).pubkey

    @profiler
    def create_routes_for_payment(
            self, *,
            amount_msat: int,        # part of payment amount we want routes for now
            final_total_msat: int,   # total payment amount final receiver will get
            invoice_pubkey,
            min_cltv_expiry,
            r_tags,
            invoice_features: int,
            payment_hash,
            payment_secret,
            trampoline_fee_level: int,
            use_two_trampolines: bool,
            fwd_trampoline_onion = None,
            full_path: LNPaymentPath = None) -> Sequence[Tuple[LNPaymentRoute, int]]:

        """Creates multiple routes for splitting a payment over the available
        private channels.

        We first try to conduct the payment over a single channel. If that fails
        and mpp is supported by the receiver, we will split the payment."""
        # It could happen that the pathfinding uses a channel
        # in the graph multiple times, meaning we could exhaust
        # its capacity. This could be dealt with by temporarily
        # iteratively blacklisting channels for this mpp attempt.
        invoice_features = LnFeatures(invoice_features)
        trampoline_features = LnFeatures.VAR_ONION_OPT
        local_height = self.network.get_local_height()
        active_channels = [chan for chan in self.channels.values() if chan.is_active() and not chan.is_frozen_for_sending()]
        try:
            # try to send over a single channel
            if not self.channel_db:
                for chan in active_channels:
                    if not self.is_trampoline_peer(chan.node_id):
                        continue
                    if chan.node_id == invoice_pubkey:
                        trampoline_onion = None
                        trampoline_payment_secret = payment_secret
                        trampoline_total_msat = final_total_msat
                        amount_with_fees = amount_msat
                        cltv_delta = min_cltv_expiry
                    else:
                        trampoline_onion, amount_with_fees, cltv_delta = create_trampoline_route_and_onion(
                            amount_msat=amount_msat,
                            total_msat=final_total_msat,
                            min_cltv_expiry=min_cltv_expiry,
                            my_pubkey=self.node_keypair.pubkey,
                            invoice_pubkey=invoice_pubkey,
                            invoice_features=invoice_features,
                            node_id=chan.node_id,
                            r_tags=r_tags,
                            payment_hash=payment_hash,
                            payment_secret=payment_secret,
                            local_height=local_height,
                            trampoline_fee_level=trampoline_fee_level,
                            use_two_trampolines=use_two_trampolines)
                        trampoline_payment_secret = os.urandom(32)
                        trampoline_total_msat = amount_with_fees
                    if chan.available_to_spend(LOCAL, strict=True) < amount_with_fees:
                        continue
                    route = [
                        RouteEdge(
                            start_node=self.node_keypair.pubkey,
                            end_node=chan.node_id,
                            short_channel_id=chan.short_channel_id,
                            fee_base_msat=0,
                            fee_proportional_millionths=0,
                            cltv_expiry_delta=0,
                            node_features=trampoline_features)
                    ]
                    routes = [(route, amount_with_fees, trampoline_total_msat, amount_msat, cltv_delta, trampoline_payment_secret, trampoline_onion)]
                    break
                else:
                    raise NoPathFound()
            else:
                route = self.create_route_for_payment(
                    amount_msat=amount_msat,
                    invoice_pubkey=invoice_pubkey,
                    min_cltv_expiry=min_cltv_expiry,
                    r_tags=r_tags,
                    invoice_features=invoice_features,
                    channels=active_channels,
                    full_path=full_path)
                routes = [(route, amount_msat, final_total_msat, amount_msat, min_cltv_expiry, payment_secret, fwd_trampoline_onion)]
        except NoPathFound:
            if not invoice_features.supports(LnFeatures.BASIC_MPP_OPT):
                raise
            channels_with_funds = {
                (chan.channel_id, chan.node_id): int(chan.available_to_spend(HTLCOwner.LOCAL))
                for chan in active_channels}
            self.logger.info(f"channels_with_funds: {channels_with_funds}")
            # for trampoline mpp payments we have to restrict ourselves to pay
            # to a single node due to some incompatibility in Eclair, see:
            # https://github.com/ACINQ/eclair/issues/1723
            use_singe_node = not self.channel_db and constants.net is constants.BitcoinMainnet
            split_configurations = suggest_splits(amount_msat, channels_with_funds, single_node=use_singe_node)
            self.logger.info(f'suggest_split {amount_msat} returned {len(split_configurations)} configurations')

            for s in split_configurations:
                self.logger.info(f"trying split configuration: {s[0].values()} rating: {s[1]}")
                routes = []
                try:
                    if not self.channel_db:
                        buckets = defaultdict(list)
                        for (chan_id, _), part_amount_msat in s[0].items():
                            chan = self.channels[chan_id]
                            if part_amount_msat:
                                buckets[chan.node_id].append((chan_id, part_amount_msat))
                        for node_id, bucket in buckets.items():
                            bucket_amount_msat = sum([x[1] for x in bucket])
                            trampoline_onion, bucket_amount_with_fees, bucket_cltv_delta = create_trampoline_route_and_onion(
                                amount_msat=bucket_amount_msat,
                                total_msat=final_total_msat,
                                min_cltv_expiry=min_cltv_expiry,
                                my_pubkey=self.node_keypair.pubkey,
                                invoice_pubkey=invoice_pubkey,
                                invoice_features=invoice_features,
                                node_id=node_id,
                                r_tags=r_tags,
                                payment_hash=payment_hash,
                                payment_secret=payment_secret,
                                local_height=local_height,
                                trampoline_fee_level=trampoline_fee_level,
                                use_two_trampolines=use_two_trampolines)
                            # node_features is only used to determine is_tlv
                            bucket_payment_secret = os.urandom(32)
                            bucket_fees = bucket_amount_with_fees - bucket_amount_msat
                            self.logger.info(f'bucket_fees {bucket_fees}')
                            for chan_id, part_amount_msat in bucket:
                                chan = self.channels[chan_id]
                                margin = chan.available_to_spend(LOCAL, strict=True) - part_amount_msat
                                delta_fee = min(bucket_fees, margin)
                                part_amount_msat_with_fees = part_amount_msat + delta_fee
                                bucket_fees -= delta_fee
                                route = [
                                    RouteEdge(
                                        start_node=self.node_keypair.pubkey,
                                        end_node=node_id,
                                        short_channel_id=chan.short_channel_id,
                                        fee_base_msat=0,
                                        fee_proportional_millionths=0,
                                        cltv_expiry_delta=0,
                                        node_features=trampoline_features)
                                ]
                                self.logger.info(f'adding route {part_amount_msat} {delta_fee} {margin}')
                                routes.append((route, part_amount_msat_with_fees, bucket_amount_with_fees, part_amount_msat, bucket_cltv_delta, bucket_payment_secret, trampoline_onion))
                            if bucket_fees != 0:
                                self.logger.info('not enough margin to pay trampoline fee')
                                raise NoPathFound()
                    else:
                        for (chan_id, _), part_amount_msat in s[0].items():
                            if part_amount_msat:
                                channel = self.channels[chan_id]
                                route = self.create_route_for_payment(
                                    amount_msat=part_amount_msat,
                                    invoice_pubkey=invoice_pubkey,
                                    min_cltv_expiry=min_cltv_expiry,
                                    r_tags=r_tags,
                                    invoice_features=invoice_features,
                                    channels=[channel],
                                    full_path=None)
                                routes.append((route, part_amount_msat, final_total_msat, part_amount_msat, min_cltv_expiry, payment_secret, fwd_trampoline_onion))
                    self.logger.info(f"found acceptable split configuration: {list(s[0].values())} rating: {s[1]}")
                    break
                except NoPathFound:
                    continue
            else:
                raise NoPathFound()
        return routes

    def create_route_for_payment(
            self, *,
            amount_msat: int,
            invoice_pubkey: bytes,
            min_cltv_expiry: int,
            r_tags,
            invoice_features: int,
            channels: List[Channel],
            full_path: Optional[LNPaymentPath]) -> Tuple[LNPaymentRoute, int]:

        scid_to_my_channels = {
            chan.short_channel_id: chan for chan in channels
            if chan.short_channel_id is not None
        }
        blacklist = self.network.channel_blacklist.get_current_list()
        # Collect all private edges from route hints.
        # Note: if some route hints are multiple edges long, and these paths cross each other,
        #       we allow our path finding to cross the paths; i.e. the route hints are not isolated.
        private_route_edges = {}  # type: Dict[ShortChannelID, RouteEdge]
        for private_path in r_tags:
            # we need to shift the node pubkey by one towards the destination:
            private_path_nodes = [edge[0] for edge in private_path][1:] + [invoice_pubkey]
            private_path_rest = [edge[1:] for edge in private_path]
            start_node = private_path[0][0]
            for end_node, edge_rest in zip(private_path_nodes, private_path_rest):
                short_channel_id, fee_base_msat, fee_proportional_millionths, cltv_expiry_delta = edge_rest
                short_channel_id = ShortChannelID(short_channel_id)
                # if we have a routing policy for this edge in the db, that takes precedence,
                # as it is likely from a previous failure
                channel_policy = self.channel_db.get_policy_for_node(
                    short_channel_id=short_channel_id,
                    node_id=start_node,
                    my_channels=scid_to_my_channels)
                if channel_policy:
                    fee_base_msat = channel_policy.fee_base_msat
                    fee_proportional_millionths = channel_policy.fee_proportional_millionths
                    cltv_expiry_delta = channel_policy.cltv_expiry_delta
                node_info = self.channel_db.get_node_info_for_node_id(node_id=end_node)
                route_edge = RouteEdge(
                        start_node=start_node,
                        end_node=end_node,
                        short_channel_id=short_channel_id,
                        fee_base_msat=fee_base_msat,
                        fee_proportional_millionths=fee_proportional_millionths,
                        cltv_expiry_delta=cltv_expiry_delta,
                        node_features=node_info.features if node_info else 0)
                if route_edge.short_channel_id not in blacklist:
                    private_route_edges[route_edge.short_channel_id] = route_edge
                start_node = end_node
        # now find a route, end to end: between us and the recipient
        try:
            route = self.network.path_finder.find_route(
                nodeA=self.node_keypair.pubkey,
                nodeB=invoice_pubkey,
                invoice_amount_msat=amount_msat,
                path=full_path,
                my_channels=scid_to_my_channels,
                blacklist=blacklist,
                private_route_edges=private_route_edges)
        except NoChannelPolicy as e:
            raise NoPathFound() from e
        if not route:
            raise NoPathFound()
        # test sanity
        if not is_route_sane_to_use(route, amount_msat, min_cltv_expiry):
            self.logger.info(f"rejecting insane route {route}")
            raise NoPathFound()
        assert len(route) > 0
        if route[-1].end_node != invoice_pubkey:
            raise LNPathInconsistent("last node_id != invoice pubkey")
        # add features from invoice
        route[-1].node_features |= invoice_features
        return route

    def add_request(self, amount_sat, message, expiry) -> str:
        coro = self._add_request_coro(amount_sat, message, expiry)
        fut = asyncio.run_coroutine_threadsafe(coro, self.network.asyncio_loop)
        try:
            return fut.result(timeout=5)
        except concurrent.futures.TimeoutError:
            raise Exception(_("add invoice timed out"))

    @log_exceptions
    async def create_invoice(
            self, *,
            amount_msat: Optional[int],
            message: str,
            expiry: int) -> Tuple[LnAddr, str]:

        timestamp = int(time.time())
        routing_hints = await self._calc_routing_hints_for_invoice(amount_msat)
        if not routing_hints:
            self.logger.info(
                "Warning. No routing hints added to invoice. "
                "Other clients will likely not be able to send to us.")
        # if not all hints are trampoline, do not create trampoline invoice
        invoice_features = self.features.for_invoice()
        trampoline_hints = []
        for r in routing_hints:
            node_id, short_channel_id, fee_base_msat, fee_proportional_millionths, cltv_expiry_delta = r[1][0]
            if len(r[1])== 1 and self.is_trampoline_peer(node_id):
                trampoline_hints.append(('t', (node_id, fee_base_msat, fee_proportional_millionths, cltv_expiry_delta)))
        payment_preimage = os.urandom(32)
        payment_hash = sha256(payment_preimage)
        info = PaymentInfo(payment_hash, amount_msat, RECEIVED, PR_UNPAID)
        amount_btc = amount_msat/Decimal(COIN*1000) if amount_msat else None
        if expiry == 0:
            expiry = LN_EXPIRY_NEVER
        lnaddr = LnAddr(
            paymenthash=payment_hash,
            amount=amount_btc,
            tags=[
                ('d', message),
                ('c', MIN_FINAL_CLTV_EXPIRY_FOR_INVOICE),
                ('x', expiry),
                ('9', invoice_features)]
            + routing_hints
            + trampoline_hints,
            date=timestamp,
            payment_secret=derive_payment_secret_from_payment_preimage(payment_preimage))
        invoice = lnencode(lnaddr, self.node_keypair.privkey)
        self.save_preimage(payment_hash, payment_preimage)
        self.save_payment_info(info)
        return lnaddr, invoice

    async def _add_request_coro(self, amount_sat: Optional[int], message, expiry: int) -> str:
        amount_msat = amount_sat * 1000 if amount_sat is not None else None
        lnaddr, invoice = await self.create_invoice(
            amount_msat=amount_msat,
            message=message,
            expiry=expiry)
        key = bh2u(lnaddr.paymenthash)
        req = LNInvoice.from_bech32(invoice)
        self.wallet.add_payment_request(req)
        self.wallet.set_label(key, message)
        return key

    def save_preimage(self, payment_hash: bytes, preimage: bytes):
        assert sha256(preimage) == payment_hash
        self.preimages[bh2u(payment_hash)] = bh2u(preimage)
        self.wallet.save_db()

    def get_preimage(self, payment_hash: bytes) -> Optional[bytes]:
        r = self.preimages.get(bh2u(payment_hash))
        return bfh(r) if r else None

    def get_payment_info(self, payment_hash: bytes) -> Optional[PaymentInfo]:
        """returns None if payment_hash is a payment we are forwarding"""
        key = payment_hash.hex()
        with self.lock:
            if key in self.payments:
                amount_msat, direction, status = self.payments[key]
                return PaymentInfo(payment_hash, amount_msat, direction, status)

    def save_payment_info(self, info: PaymentInfo) -> None:
        key = info.payment_hash.hex()
        assert info.status in SAVED_PR_STATUS
        with self.lock:
            self.payments[key] = info.amount_msat, info.direction, info.status
        self.wallet.save_db()

    def check_received_mpp_htlc(self, payment_secret, short_channel_id, htlc: UpdateAddHtlc, expected_msat: int) -> Optional[bool]:
        """ return MPP status: True (accepted), False (expired) or None """
        payment_hash = htlc.payment_hash
        is_expired, is_accepted, htlc_set = self.received_mpp_htlcs.get(payment_secret, (False, False, set()))
        if self.get_payment_status(payment_hash) == PR_PAID:
            # payment_status is persisted
            is_accepted = True
            is_expired = False
        key = (short_channel_id, htlc)
        if key not in htlc_set:
            htlc_set.add(key)
        if not is_accepted and not is_expired:
            total = sum([_htlc.amount_msat for scid, _htlc in htlc_set])
            first_timestamp = min([_htlc.timestamp for scid, _htlc in htlc_set])
            if self.stopping_soon:
                is_expired = True  # try to time out pending HTLCs before shutting down
            elif time.time() - first_timestamp > self.MPP_EXPIRY:
                is_expired = True
            elif total == expected_msat:
                is_accepted = True
        if is_accepted or is_expired:
            htlc_set.remove(key)
        if len(htlc_set) > 0:
            self.received_mpp_htlcs[payment_secret] = is_expired, is_accepted, htlc_set
        elif payment_secret in self.received_mpp_htlcs:
            self.received_mpp_htlcs.pop(payment_secret)
        return True if is_accepted else (False if is_expired else None)

    def get_payment_status(self, payment_hash: bytes) -> int:
        info = self.get_payment_info(payment_hash)
        return info.status if info else PR_UNPAID

    def get_invoice_status(self, invoice: LNInvoice) -> int:
        key = invoice.rhash
        log = self.logs[key]
        if key in self.inflight_payments:
            return PR_INFLIGHT
        # status may be PR_FAILED
        status = self.get_payment_status(bfh(key))
        if status == PR_UNPAID and log:
            status = PR_FAILED
        return status

    def set_invoice_status(self, key: str, status: int) -> None:
        if status == PR_INFLIGHT:
            self.inflight_payments.add(key)
        elif key in self.inflight_payments:
            self.inflight_payments.remove(key)
        if status in SAVED_PR_STATUS:
            self.set_payment_status(bfh(key), status)
        util.trigger_callback('invoice_status', self.wallet, key)

    def set_request_status(self, payment_hash: bytes, status: int) -> None:
        if self.get_payment_status(payment_hash) != status:
            self.set_payment_status(payment_hash, status)
            util.trigger_callback('request_status', self.wallet, payment_hash.hex(), status)

    def set_payment_status(self, payment_hash: bytes, status: int) -> None:
        info = self.get_payment_info(payment_hash)
        if info is None:
            # if we are forwarding
            return
        info = info._replace(status=status)
        self.save_payment_info(info)

    def htlc_fulfilled(self, chan, payment_hash: bytes, htlc_id:int):
        util.trigger_callback('htlc_fulfilled', payment_hash, chan.channel_id)
        q = self.sent_htlcs.get(payment_hash)
        if q:
            route, payment_secret, amount_msat, bucket_msat, amount_receiver_msat = self.sent_htlcs_routes[(payment_hash, chan.short_channel_id, htlc_id)]
            htlc_log = HtlcLog(
                success=True,
                route=route,
                amount_msat=amount_receiver_msat)
            q.put_nowait(htlc_log)
        else:
            key = payment_hash.hex()
            self.set_invoice_status(key, PR_PAID)
            util.trigger_callback('payment_succeeded', self.wallet, key)

    def htlc_failed(
            self,
            chan: Channel,
            payment_hash: bytes,
            htlc_id: int,
            error_bytes: Optional[bytes],
            failure_message: Optional['OnionRoutingFailure']):

        util.trigger_callback('htlc_failed', payment_hash, chan.channel_id)
        q = self.sent_htlcs.get(payment_hash)
        if q:
            # detect if it is part of a bucket
            # if yes, wait until the bucket completely failed
            key = (payment_hash, chan.short_channel_id, htlc_id)
            route, payment_secret, amount_msat, bucket_msat, amount_receiver_msat = self.sent_htlcs_routes[key]
            if error_bytes:
                # TODO "decode_onion_error" might raise, catch and maybe blacklist/penalise someone?
                try:
                    failure_message, sender_idx = chan.decode_onion_error(error_bytes, route, htlc_id)
                except Exception as e:
                    sender_idx = None
                    failure_message = OnionRoutingFailure(-1, str(e))
            else:
                # probably got "update_fail_malformed_htlc". well... who to penalise now?
                assert failure_message is not None
                sender_idx = None
            self.logger.info(f"htlc_failed {failure_message}")

            # check sent_buckets if we use trampoline
            if not self.channel_db and payment_secret in self.sent_buckets:
                amount_sent, amount_failed = self.sent_buckets[payment_secret]
                amount_failed += amount_receiver_msat
                self.sent_buckets[payment_secret] = amount_sent, amount_failed
                if amount_sent != amount_failed:
                    self.logger.info('bucket still active...')
                    return
                self.logger.info('bucket failed')
                amount_receiver_msat = amount_sent

            htlc_log = HtlcLog(
                success=False,
                route=route,
                amount_msat=amount_receiver_msat,
                error_bytes=error_bytes,
                failure_msg=failure_message,
                sender_idx=sender_idx)
            q.put_nowait(htlc_log)
        else:
            self.logger.info(f"received unknown htlc_failed, probably from previous session")
            key = payment_hash.hex()
            self.set_invoice_status(key, PR_UNPAID)
            util.trigger_callback('payment_failed', self.wallet, key, '')

    async def _calc_routing_hints_for_invoice(self, amount_msat: Optional[int]):
        """calculate routing hints (BOLT-11 'r' field)"""
        routing_hints = []
        channels = list(self.channels.values())
        # do minimal filtering of channels.
        # we include channels that cannot *right now* receive (e.g. peer disconnected or balance insufficient)
        channels = [chan for chan in channels
                    if (chan.is_open() and not chan.is_frozen_for_receiving())]
        # cap max channels to include to keep QR code reasonably scannable
        channels = sorted(channels, key=lambda chan: (not chan.is_active(), -chan.available_to_spend(REMOTE)))
        channels = channels[:15]
        random.shuffle(channels)  # let's not leak channel order
        scid_to_my_channels = {chan.short_channel_id: chan for chan in channels
                               if chan.short_channel_id is not None}
        for chan in channels:
            chan_id = chan.short_channel_id
            assert isinstance(chan_id, bytes), chan_id
            channel_info = get_mychannel_info(chan_id, scid_to_my_channels)
            # note: as a fallback, if we don't have a channel update for the
            # incoming direction of our private channel, we fill the invoice with garbage.
            # the sender should still be able to pay us, but will incur an extra round trip
            # (they will get the channel update from the onion error)
            # at least, that's the theory. https://github.com/lightningnetwork/lnd/issues/2066
            fee_base_msat = fee_proportional_millionths = 0
            cltv_expiry_delta = 1  # lnd won't even try with zero
            missing_info = True
            if channel_info:
                policy = get_mychannel_policy(channel_info.short_channel_id, chan.node_id, scid_to_my_channels)
                if policy:
                    fee_base_msat = policy.fee_base_msat
                    fee_proportional_millionths = policy.fee_proportional_millionths
                    cltv_expiry_delta = policy.cltv_expiry_delta
                    missing_info = False
            if missing_info:
                self.logger.info(
                    f"Warning. Missing channel update for our channel {chan_id}; "
                    f"filling invoice with incorrect data.")
            routing_hints.append(('r', [(
                chan.node_id,
                chan_id,
                fee_base_msat,
                fee_proportional_millionths,
                cltv_expiry_delta)]))
        return routing_hints

    def delete_payment(self, payment_hash_hex: str):
        try:
            with self.lock:
                del self.payments[payment_hash_hex]
        except KeyError:
            return
        self.wallet.save_db()

    def get_balance(self):
        with self.lock:
            return Decimal(sum(
                chan.balance(LOCAL) if not chan.is_closed() else 0
                for chan in self.channels.values())) / 1000

    def num_sats_can_send(self) -> Decimal:
        can_send = 0
        with self.lock:
            if self.channels:
                for c in self.channels.values():
                    if c.is_active() and not c.is_frozen_for_sending():
                        can_send += c.available_to_spend(LOCAL)
        # Here we have to guess a fee, because some callers (submarine swaps)
        # use this method to initiate a payment, which would otherwise fail.
        fee_base_msat = TRAMPOLINE_FEES[3]['fee_base_msat']
        fee_proportional_millionths = TRAMPOLINE_FEES[3]['fee_proportional_millionths']
        # inverse of fee_for_edge_msat
        can_send_minus_fees = (can_send - fee_base_msat) * 1_000_000 // ( 1_000_000 + fee_proportional_millionths)
        can_send_minus_fees = max(0, can_send_minus_fees)
        return Decimal(can_send_minus_fees) / 1000

    def num_sats_can_receive(self) -> Decimal:
        can_receive = 0
        with self.lock:
            if self.channels:
                for c in self.channels.values():
                    if c.is_active() and not c.is_frozen_for_receiving():
                        can_receive += c.available_to_spend(REMOTE)
        return Decimal(can_receive) / 1000

    def num_sats_can_receive_no_mpp(self) -> Decimal:
        can_receive = 0
        with self.lock:
            if self.channels:
                can_receive = max([
                    c.available_to_spend(REMOTE) for c in self.channels.values()
                    if c.is_active() and not c.is_frozen_for_receiving()
                ])
        return Decimal(can_receive) / 1000

    def can_pay_invoice(self, invoice: LNInvoice) -> bool:
        return invoice.get_amount_sat() <= self.num_sats_can_send()

    def can_receive_invoice(self, invoice: LNInvoice) -> bool:
        return invoice.get_amount_sat() <= self.num_sats_can_receive()

    async def close_channel(self, chan_id):
        chan = self._channels[chan_id]
        peer = self._peers[chan.node_id]
        return await peer.close_channel(chan_id)

    async def force_close_channel(self, chan_id):
        # returns txid or raises
        chan = self._channels[chan_id]
        tx = chan.force_close_tx()
        await self.network.broadcast_transaction(tx)
        chan.set_state(ChannelState.FORCE_CLOSING)
        return tx.txid()

    async def try_force_closing(self, chan_id):
        # fails silently but sets the state, so that we will retry later
        chan = self._channels[chan_id]
        tx = chan.force_close_tx()
        chan.set_state(ChannelState.FORCE_CLOSING)
        await self.network.try_broadcasting(tx, 'force-close')

    def remove_channel(self, chan_id):
        chan = self.channels[chan_id]
        assert chan.can_be_deleted()
        with self.lock:
            self._channels.pop(chan_id)
            self.db.get('channels').pop(chan_id.hex())
        for addr in chan.get_wallet_addresses_channel_might_want_reserved():
            self.wallet.set_reserved_state_of_address(addr, reserved=False)

        util.trigger_callback('channels_updated', self.wallet)
        util.trigger_callback('wallet_updated', self.wallet)

    @ignore_exceptions
    @log_exceptions
    async def reestablish_peer_for_given_channel(self, chan: Channel) -> None:
        now = time.time()
        peer_addresses = []
        if not self.channel_db:
            addr = trampolines_by_id().get(chan.node_id)
            if addr:
                peer_addresses.append(addr)
        else:
            # will try last good address first, from gossip
            last_good_addr = self.channel_db.get_last_good_address(chan.node_id)
            if last_good_addr:
                peer_addresses.append(last_good_addr)
            # will try addresses for node_id from gossip
            addrs_from_gossip = self.channel_db.get_node_addresses(chan.node_id) or []
            for host, port, ts in addrs_from_gossip:
                peer_addresses.append(LNPeerAddr(host, port, chan.node_id))
        # will try addresses stored in channel storage
        peer_addresses += list(chan.get_peer_addresses())
        # Done gathering addresses.
        # Now select first one that has not failed recently.
        for peer in peer_addresses:
            if self._can_retry_addr(peer, urgent=True, now=now):
                await self._add_peer(peer.host, peer.port, peer.pubkey)
                return

    async def reestablish_peers_and_channels(self):
        while True:
            await asyncio.sleep(1)
            if self.stopping_soon:
                return
            for chan in self.channels.values():
                if chan.is_closed():
                    continue
                # reestablish
                if not chan.should_try_to_reestablish_peer():
                    continue
                peer = self._peers.get(chan.node_id, None)
                if peer:
                    await peer.taskgroup.spawn(peer.reestablish_channel(chan))
                else:
                    await self.taskgroup.spawn(self.reestablish_peer_for_given_channel(chan))

    def current_feerate_per_kw(self):
        from .simple_config import FEE_LN_ETA_TARGET, FEERATE_FALLBACK_STATIC_FEE, FEERATE_REGTEST_HARDCODED
        if constants.net is constants.BitcoinRegtest:
            return FEERATE_REGTEST_HARDCODED // 4
        feerate_per_kvbyte = self.network.config.eta_target_to_fee(FEE_LN_ETA_TARGET)
        if feerate_per_kvbyte is None:
            feerate_per_kvbyte = FEERATE_FALLBACK_STATIC_FEE
        return max(253, feerate_per_kvbyte // 4)

    def create_channel_backup(self, channel_id):
        chan = self._channels[channel_id]
        # do not backup old-style channels
        assert chan.is_static_remotekey_enabled()
        peer_addresses = list(chan.get_peer_addresses())
        peer_addr = peer_addresses[0]
        return ImportedChannelBackupStorage(
            node_id = chan.node_id,
            privkey = self.node_keypair.privkey,
            funding_txid = chan.funding_outpoint.txid,
            funding_index = chan.funding_outpoint.output_index,
            funding_address = chan.get_funding_address(),
            host = peer_addr.host,
            port = peer_addr.port,
            is_initiator = chan.constraints.is_initiator,
            channel_seed = chan.config[LOCAL].channel_seed,
            local_delay = chan.config[LOCAL].to_self_delay,
            remote_delay = chan.config[REMOTE].to_self_delay,
            remote_revocation_pubkey = chan.config[REMOTE].revocation_basepoint.pubkey,
            remote_payment_pubkey = chan.config[REMOTE].payment_basepoint.pubkey)

    def export_channel_backup(self, channel_id):
        xpub = self.wallet.get_fingerprint()
        backup_bytes = self.create_channel_backup(channel_id).to_bytes()
        assert backup_bytes == ImportedChannelBackupStorage.from_bytes(backup_bytes).to_bytes(), "roundtrip failed"
        encrypted = pw_encode_with_version_and_mac(backup_bytes, xpub)
        assert backup_bytes == pw_decode_with_version_and_mac(encrypted, xpub), "encrypt failed"
        return 'channel_backup:' + encrypted

    async def request_force_close(self, channel_id: bytes, *, connect_str=None) -> None:
        if channel_id in self.channels:
            chan = self.channels[channel_id]
            peer = self._peers.get(chan.node_id)
            if not peer:
                raise Exception('Peer not found')
            chan.should_request_force_close = True
            peer.close_and_cleanup()
        elif connect_str:
            peer = await self.add_peer(connect_str)
            await peer.trigger_force_close(channel_id)
        elif channel_id in self.channel_backups:
            await self._request_force_close_from_backup(channel_id)
        else:
            raise Exception(f'Unknown channel {channel_id.hex()}')

    def import_channel_backup(self, data):
        assert data.startswith('channel_backup:')
        encrypted = data[15:]
        xpub = self.wallet.get_fingerprint()
        decrypted = pw_decode_with_version_and_mac(encrypted, xpub)
        cb_storage = ImportedChannelBackupStorage.from_bytes(decrypted)
        channel_id = cb_storage.channel_id()
        if channel_id.hex() in self.db.get_dict("channels"):
            raise Exception('Channel already in wallet')
        self.logger.info(f'importing channel backup: {channel_id.hex()}')
        d = self.db.get_dict("imported_channel_backups")
        d[channel_id.hex()] = cb_storage
        with self.lock:
            cb = ChannelBackup(cb_storage, sweep_address=self.sweep_address, lnworker=self)
            self._channel_backups[channel_id] = cb
        self.wallet.save_db()
        util.trigger_callback('channels_updated', self.wallet)
        self.lnwatcher.add_channel(cb.funding_outpoint.to_str(), cb.get_funding_address())

    def has_conflicting_backup_with(self, remote_node_id: bytes):
        """ Returns whether we have an active channel with this node on another device, using same local node id. """
        channel_backup_peers = [
            cb.node_id for cb in self.channel_backups.values()
            if (not cb.is_closed() and cb.get_local_pubkey() == self.node_keypair.pubkey)]
        return any(remote_node_id.startswith(cb_peer_nodeid) for cb_peer_nodeid in channel_backup_peers)

    def remove_channel_backup(self, channel_id):
        chan = self.channel_backups[channel_id]
        assert chan.can_be_deleted()
        onchain_backups = self.db.get_dict("onchain_channel_backups")
        imported_backups = self.db.get_dict("imported_channel_backups")
        if channel_id.hex() in onchain_backups:
            onchain_backups.pop(channel_id.hex())
        elif channel_id.hex() in imported_backups:
            imported_backups.pop(channel_id.hex())
        else:
            raise Exception('Channel not found')
        with self.lock:
            self._channel_backups.pop(channel_id)
        self.wallet.save_db()
        util.trigger_callback('channels_updated', self.wallet)

    @log_exceptions
    async def _request_force_close_from_backup(self, channel_id: bytes):
        cb = self.channel_backups.get(channel_id)
        if not cb:
            raise Exception(f'channel backup not found {self.channel_backups}')
        cb = cb.cb # storage
        self.logger.info(f'requesting channel force close: {channel_id.hex()}')
        if isinstance(cb, ImportedChannelBackupStorage):
            node_id = cb.node_id
            privkey = cb.privkey
            addresses = [(cb.host, cb.port, 0)]
            # TODO also try network addresses from gossip db (as it might have changed)
        else:
            assert isinstance(cb, OnchainChannelBackupStorage)
            if not self.channel_db:
                raise Exception('Enable gossip first')
            node_id = self.network.channel_db.get_node_by_prefix(cb.node_id_prefix)
            privkey = self.node_keypair.privkey
            addresses = self.network.channel_db.get_node_addresses(node_id)
            if not addresses:
                raise Exception('Peer not found in gossip database')
        for host, port, timestamp in addresses:
            peer_addr = LNPeerAddr(host, port, node_id)
            transport = LNTransport(privkey, peer_addr, proxy=self.network.proxy)
            peer = Peer(self, node_id, transport, is_channel_backup=True)
            try:
                async with TaskGroup(wait=any) as group:
                    await group.spawn(peer._message_loop())
                    await group.spawn(peer.trigger_force_close(channel_id))
                return
            except Exception as e:
                self.logger.info(f'failed to connect {host} {e}')
                continue
            # TODO close/cleanup the transport
        else:
            raise Exception('failed to connect')

    def maybe_add_backup_from_tx(self, tx):
        funding_address = None
        node_id_prefix = None
        for i, o in enumerate(tx.outputs()):
            script_type = get_script_type_from_output_script(o.scriptpubkey)
            if script_type == 'p2wsh':
                funding_index = i
                funding_address = o.address
                for o2 in tx.outputs():
                    if o2.scriptpubkey.startswith(bytes([opcodes.OP_RETURN])):
                        encrypted_data = o2.scriptpubkey[2:]
                        data = self.decrypt_cb_data(encrypted_data, funding_address)
                        if data.startswith(CB_MAGIC_BYTES):
                            node_id_prefix = data[4:]
        if node_id_prefix is None:
            return
        funding_txid = tx.txid()
        cb_storage = OnchainChannelBackupStorage(
            node_id_prefix = node_id_prefix,
            funding_txid = funding_txid,
            funding_index = funding_index,
            funding_address = funding_address,
            is_initiator = True)
        channel_id = cb_storage.channel_id().hex()
        if channel_id in self.db.get_dict("channels"):
            return
        self.logger.info(f"adding backup from tx")
        d = self.db.get_dict("onchain_channel_backups")
        d[channel_id] = cb_storage
        cb = ChannelBackup(cb_storage, sweep_address=self.sweep_address, lnworker=self)
        self.wallet.save_db()
        with self.lock:
            self._channel_backups[bfh(channel_id)] = cb
        util.trigger_callback('channels_updated', self.wallet)
        self.lnwatcher.add_channel(cb.funding_outpoint.to_str(), cb.get_funding_address())
