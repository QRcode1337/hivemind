import asyncio
import multiprocessing as mp
import random
import heapq
from typing import Optional
import numpy as np

import hivemind
from typing import List, Dict

from hivemind import get_dht_time
from hivemind.dht.node import DHTID, Endpoint, DHTNode, LOCALHOST, DHTProtocol


def run_protocol_listener(port: int, dhtid: DHTID, started: mp.synchronize.Event, ping: Optional[Endpoint] = None):
    loop = asyncio.get_event_loop()
    protocol = loop.run_until_complete(DHTProtocol.create(
        dhtid, bucket_size=20, depth_modulo=5, num_replicas=3, wait_timeout=5, listen_on=f"{LOCALHOST}:{port}"))

    assert protocol.port == port
    print(f"Started peer id={protocol.node_id} port={port}", flush=True)

    if ping is not None:
        loop.run_until_complete(protocol.call_ping(ping))
    started.set()
    loop.run_until_complete(protocol.server.wait_for_termination())
    print(f"Finished peer id={protocol.node_id} port={port}", flush=True)


def test_dht_protocol():
    # create the first peer
    peer1_port, peer1_id, peer1_started = hivemind.find_open_port(), DHTID.generate(), mp.Event()
    peer1_proc = mp.Process(target=run_protocol_listener, args=(peer1_port, peer1_id, peer1_started), daemon=True)
    peer1_proc.start(), peer1_started.wait()

    # create another peer that connects to the first peer
    peer2_port, peer2_id, peer2_started = hivemind.find_open_port(), DHTID.generate(), mp.Event()
    peer2_proc = mp.Process(target=run_protocol_listener, args=(peer2_port, peer2_id, peer2_started),
                            kwargs={'ping': f'{LOCALHOST}:{peer1_port}'}, daemon=True)
    peer2_proc.start(), peer2_started.wait()

    test_success = mp.Event()

    def _tester():
        # note: we run everything in a separate process to re-initialize all global states from scratch
        # this helps us avoid undesirable side-effects when running multiple tests in sequence

        loop = asyncio.get_event_loop()
        for listen in [False, True]:  # note: order matters, this test assumes that first run uses listen=False
            protocol = loop.run_until_complete(DHTProtocol.create(
                DHTID.generate(), bucket_size=20, depth_modulo=5, wait_timeout=5, num_replicas=3, listen=listen))
            print(f"Self id={protocol.node_id}", flush=True)

            assert loop.run_until_complete(protocol.call_ping(f'{LOCALHOST}:{peer1_port}')) == peer1_id

            key, value, expiration = DHTID.generate(), [random.random(), {'ololo': 'pyshpysh'}], get_dht_time() + 1e3
            store_ok = loop.run_until_complete(protocol.call_store(
                f'{LOCALHOST}:{peer1_port}', [key], [hivemind.MSGPackSerializer.dumps(value)], expiration)
            )
            assert all(store_ok), "DHT rejected a trivial store"

            # peer 1 must know about peer 2
            recv_value_bytes, recv_expiration, nodes_found = loop.run_until_complete(
                protocol.call_find(f'{LOCALHOST}:{peer1_port}', [key]))[key]
            recv_value = hivemind.MSGPackSerializer.loads(recv_value_bytes)
            (recv_id, recv_endpoint) = next(iter(nodes_found.items()))
            assert recv_id == peer2_id and ':'.join(recv_endpoint.split(':')[-2:]) == f"{LOCALHOST}:{peer2_port}", \
                f"expected id={peer2_id}, peer={LOCALHOST}:{peer2_port} but got {recv_id}, {recv_endpoint}"

            assert recv_value == value and recv_expiration == expiration, \
                f"call_find_value expected {value} (expires by {expiration}) " \
                f"but got {recv_value} (expires by {recv_expiration})"

            # peer 2 must know about peer 1, but not have a *random* nonexistent value
            dummy_key = DHTID.generate()
            recv_dummy_value, recv_dummy_expiration, nodes_found_2 = loop.run_until_complete(
                protocol.call_find(f'{LOCALHOST}:{peer2_port}', [dummy_key]))[dummy_key]
            assert recv_dummy_value is None and recv_dummy_expiration is None, "Non-existent keys shouldn't have values"
            (recv_id, recv_endpoint) = next(iter(nodes_found_2.items()))
            assert recv_id == peer1_id and recv_endpoint == f"{LOCALHOST}:{peer1_port}", \
                f"expected id={peer1_id}, peer={LOCALHOST}:{peer1_port} but got {recv_id}, {recv_endpoint}"

            # cause a non-response by querying a nonexistent peer
            dummy_port = hivemind.find_open_port()
            assert loop.run_until_complete(protocol.call_find(f"{LOCALHOST}:{dummy_port}", [key])) is None

            if listen:
                loop.run_until_complete(protocol.shutdown())
            print("DHTProtocol test finished successfully!")
            test_success.set()

    tester = mp.Process(target=_tester, daemon=True)
    tester.start()
    tester.join()
    assert test_success.is_set()
    peer1_proc.terminate()
    peer2_proc.terminate()


def test_empty_table():
    """ Test RPC methods with empty routing table """
    peer_port, peer_id, peer_started = hivemind.find_open_port(), DHTID.generate(), mp.Event()
    peer_proc = mp.Process(target=run_protocol_listener, args=(peer_port, peer_id, peer_started), daemon=True)
    peer_proc.start(), peer_started.wait()
    test_success = mp.Event()

    def _tester():
        # note: we run everything in a separate process to re-initialize all global states from scratch
        # this helps us avoid undesirable side-effects when running multiple tests in sequence

        loop = asyncio.get_event_loop()
        protocol = loop.run_until_complete(DHTProtocol.create(
            DHTID.generate(), bucket_size=20, depth_modulo=5, wait_timeout=5, num_replicas=3, listen=False))

        key, value, expiration = DHTID.generate(), [random.random(), {'ololo': 'pyshpysh'}], get_dht_time() + 1e3

        recv_value_bytes, recv_expiration, nodes_found = loop.run_until_complete(
            protocol.call_find(f'{LOCALHOST}:{peer_port}', [key]))[key]
        assert recv_value_bytes is None and recv_expiration is None and len(nodes_found) == 0
        assert all(loop.run_until_complete(protocol.call_store(
            f'{LOCALHOST}:{peer_port}', [key], [hivemind.MSGPackSerializer.dumps(value)], expiration)
        )), "peer rejected store"

        recv_value_bytes, recv_expiration, nodes_found = loop.run_until_complete(
            protocol.call_find(f'{LOCALHOST}:{peer_port}', [key]))[key]
        recv_value = hivemind.MSGPackSerializer.loads(recv_value_bytes)
        assert len(nodes_found) == 0
        assert recv_value == value and recv_expiration == expiration, "call_find_value expected " \
            f"{value} (expires by {expiration}) but got {recv_value} (expires by {recv_expiration})"

        assert loop.run_until_complete(protocol.call_ping(f'{LOCALHOST}:{peer_port}')) == peer_id
        assert loop.run_until_complete(protocol.call_ping(f'{LOCALHOST}:{hivemind.find_open_port()}')) is None
        test_success.set()

    tester = mp.Process(target=_tester, daemon=True)
    tester.start()
    tester.join()
    assert test_success.is_set()
    peer_proc.terminate()


def run_node(node_id, peers, status_pipe: mp.Pipe):
    if asyncio.get_event_loop().is_running():
        asyncio.get_event_loop().stop()  # if we're in jupyter, get rid of its built-in event loop
        asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()
    node = loop.run_until_complete(DHTNode.create(node_id, initial_peers=peers))
    status_pipe.send(node.port)
    while True:
        loop.run_forever()


def test_dht_node():
    # create dht with 50 nodes + your 51-st node
    dht: Dict[Endpoint, DHTID] = {}
    processes: List[mp.Process] = []

    for i in range(50):
        node_id = DHTID.generate()
        peers = random.sample(dht.keys(), min(len(dht), 5))
        pipe_recv, pipe_send = mp.Pipe(duplex=False)
        proc = mp.Process(target=run_node, args=(node_id, peers, pipe_send), daemon=True)
        proc.start()
        port = pipe_recv.recv()
        processes.append(proc)
        dht[f"{LOCALHOST}:{port}"] = node_id

    test_success = mp.Event()

    def _tester():
        # note: we run everything in a separate process to re-initialize all global states from scratch
        # this helps us avoid undesirable side-effects when running multiple tests in sequence
        loop = asyncio.get_event_loop()
        me = loop.run_until_complete(DHTNode.create(initial_peers=random.sample(dht.keys(), 5), parallel_rpc=10))

        # test 1: find self
        nearest = loop.run_until_complete(me.find_nearest_nodes([me.node_id], k_nearest=1))[me.node_id]
        assert len(nearest) == 1 and ':'.join(nearest[me.node_id].split(':')[-2:]) == f"{LOCALHOST}:{me.port}"

        # test 2: find others
        for i in range(10):
            ref_endpoint, query_id = random.choice(list(dht.items()))
            nearest = loop.run_until_complete(me.find_nearest_nodes([query_id], k_nearest=1))[query_id]
            assert len(nearest) == 1
            found_node_id, found_endpoint = next(iter(nearest.items()))
            assert found_node_id == query_id and ':'.join(found_endpoint.split(':')[-2:]) == ref_endpoint

        # test 3: find neighbors to random nodes
        accuracy_numerator = accuracy_denominator = 0  # top-1 nearest neighbor accuracy
        jaccard_numerator = jaccard_denominator = 0  # jaccard similarity aka intersection over union
        all_node_ids = list(dht.values())

        for i in range(100):
            query_id = DHTID.generate()
            k_nearest = random.randint(1, 20)
            exclude_self = random.random() > 0.5
            nearest = loop.run_until_complete(
                me.find_nearest_nodes([query_id], k_nearest=k_nearest, exclude_self=exclude_self))[query_id]
            nearest_nodes = list(nearest)  # keys from ordered dict

            assert len(nearest_nodes) == k_nearest, "beam search must return exactly k_nearest results"
            assert me.node_id not in nearest_nodes or not exclude_self, "if exclude, results shouldn't contain self"
            assert np.all(np.diff(query_id.xor_distance(nearest_nodes)) >= 0), "results must be sorted by distance"

            ref_nearest = heapq.nsmallest(k_nearest + 1, all_node_ids, key=query_id.xor_distance)
            if exclude_self and me.node_id in ref_nearest:
                ref_nearest.remove(me.node_id)
            if len(ref_nearest) > k_nearest:
                ref_nearest.pop()

            accuracy_numerator += nearest_nodes[0] == ref_nearest[0]
            accuracy_denominator += 1

            jaccard_numerator += len(set.intersection(set(nearest_nodes), set(ref_nearest)))
            jaccard_denominator += k_nearest

        accuracy = accuracy_numerator / accuracy_denominator
        print("Top-1 accuracy:", accuracy)  # should be 98-100%
        jaccard_index = jaccard_numerator / jaccard_denominator
        print("Jaccard index (intersection over union):", jaccard_index)  # should be 95-100%
        assert accuracy >= 0.9, f"Top-1 accuracy only {accuracy} ({accuracy_numerator} / {accuracy_denominator})"
        assert jaccard_index >= 0.9, f"Jaccard index only {accuracy} ({accuracy_numerator} / {accuracy_denominator})"

        # test 4: find all nodes
        dummy = DHTID.generate()
        nearest = loop.run_until_complete(me.find_nearest_nodes([dummy], k_nearest=len(dht) + 100))[dummy]
        assert len(nearest) == len(dht) + 1
        assert len(set.difference(set(nearest.keys()), set(all_node_ids) | {me.node_id})) == 0

        # test 5: node without peers
        other_node = loop.run_until_complete(DHTNode.create())
        nearest = loop.run_until_complete(other_node.find_nearest_nodes([dummy]))[dummy]
        assert len(nearest) == 1 and nearest[other_node.node_id] == f"{LOCALHOST}:{other_node.port}"
        nearest = loop.run_until_complete(other_node.find_nearest_nodes([dummy], exclude_self=True))[dummy]
        assert len(nearest) == 0

        # test 6 store and get value
        true_time = get_dht_time() + 1200
        assert loop.run_until_complete(me.store("mykey", ["Value", 10], true_time))
        for node in [me, other_node]:
            val, expiration_time = loop.run_until_complete(me.get("mykey"))
            assert expiration_time == true_time, "Wrong time"
            assert val == ["Value", 10], "Wrong value"

        # test 7: bulk store and bulk get
        keys = 'foo', 'bar', 'baz', 'zzz'
        values = 3, 2, 'batman', [1, 2, 3]
        store_ok = loop.run_until_complete(me.store_many(keys, values, expiration_time=get_dht_time() + 999))
        assert all(store_ok.values()), "failed to store one or more keys"
        response = loop.run_until_complete(me.get_many(keys[::-1]))
        for key, value in zip(keys, values):
            assert key in response and response[key][0] == value

        test_success.set()

    tester = mp.Process(target=_tester, daemon=True)
    tester.start()
    tester.join()
    assert test_success.is_set()
    for proc in processes:
        proc.terminate()


def test_dhtnode_caching(T=0.05):
    test_success = mp.Event()

    async def _tester():
        node2 = await hivemind.DHTNode.create(cache_refresh_before_expiry=5 * T)
        node1 = await hivemind.DHTNode.create(initial_peers=[f'localhost:{node2.port}'],
                                              cache_refresh_before_expiry=5 * T, listen=False)
        await node2.store('k', [123, 'value'], expiration_time=hivemind.get_dht_time() + 7 * T)
        await node2.store('k2', [654, 'value'], expiration_time=hivemind.get_dht_time() + 7 * T)
        await node2.store('k3', [654, 'value'], expiration_time=hivemind.get_dht_time() + 15 * T)
        await node1.get_many(['k', 'k2', 'k3', 'k4'])
        assert len(node1.protocol.cache) == 3
        assert len(node1.cache_refresh_queue) == 0

        await node1.get_many(['k', 'k2', 'k3', 'k4'])
        assert len(node1.cache_refresh_queue) == 3

        await node2.store('k', [123, 'value'], expiration_time=hivemind.get_dht_time() + 10 * T)
        await asyncio.sleep(5 * T)
        await node1.get('k')
        assert len(node1.protocol.cache) == 3
        assert len(node1.cache_refresh_queue) == 2
        await asyncio.sleep(3 * T)

        assert len(node1.cache_refresh_queue) == 1

        await asyncio.sleep(5 * T)
        assert len(node1.cache_refresh_queue) == 0
        await asyncio.sleep(5 * T)
        assert len(node1.cache_refresh_queue) == 0

        await node2.store('k', [123, 'value'], expiration_time=hivemind.get_dht_time() + 7 * T)
        await node1.get('k')
        assert len(node1.cache_refresh_queue) == 0
        await node1.get('k')
        assert len(node1.cache_refresh_queue) == 1

        await asyncio.sleep(5 * T)
        assert len(node1.cache_refresh_queue) == 0

        await asyncio.gather(node1.shutdown(), node2.shutdown())
        test_success.set()

    proc = mp.Process(target=lambda: asyncio.run(_tester()))
    proc.start()
    proc.join()
    assert test_success.is_set()


def test_dhtnode_reuse_get():
    return
    #TODO monkey-patch replace protocol.call_find to add manual delay
    test_success = mp.Event()

    async def _tester():
        peers = []
        for i in range(10):
            neighbors_i = [f'{LOCALHOST}:{node.port}' for node in random.sample(peers, min(3, len(peers)))]
            peers.append(await hivemind.DHTNode.create(initial_peers=neighbors_i, parallel_rpc=256))

        await random.choice(peers).store('k1', 123, hivemind.get_dht_time() + 999)
        await random.choice(peers).store('k2', 567, hivemind.get_dht_time() + 999)

        you = random.choice(peers)

        futures1, futures2, futures3 = await asyncio.gather(
            you.get_many(['k1', 'k2'], return_futures=True),
            you.get_many(['k2', 'k3'], return_futures=True),
            you.get_many(['k3'], return_futures=True)
        )

        assert futures1['k2'] == futures2['k2']
        assert (await futures1['k2'])[0] == 567
        assert futures1['k2'] != futures2['k3']
        assert futures2['k3'] == futures3['k3']
        assert (await futures3['k3'])[0] is None
        test_success.set()

    proc = mp.Process(target=lambda: asyncio.run(_tester()))
    proc.start()
    proc.join()
    assert test_success.is_set()
