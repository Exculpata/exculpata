"""Causal DNS proof and a reachable, deliberately forwarding negative control."""
import http.client
import json
import os
import sys

from dns_boundary_probe import query, verify


def count():
    connection = http.client.HTTPConnection(os.environ['DNS_RECORDER_ADDRESS'], 8080, timeout=3)
    try:
        connection.request('GET', '/dns-count')
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read(4096))['queries']
    finally:
        connection.close()


def main():
    mode = sys.argv[1]
    before = count()
    if mode == 'controls':
        for protocol in ('udp', 'tcp'):
            for kind in (1, 28, 16):
                assert query('synthetic-control.example.invalid', kind, protocol,
                             os.environ['DNS_RECORDER_ADDRESS']) == (3, 0)
        assert count() == before + 6
        result = {'reachable_recorder_app_controls': 6}
    elif mode == 'forwarding-control':
        # App's disposable DNS override now names the reachable recorder.
        try:
            verify(['egress-proxy'])
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError('known-forwarding DNS configuration falsely passed')
        assert count() > before, 'negative control never reached recorder'
        result = {'known_forwarding_control_rejected': True, 'forwarded_queries_observed': count() - before}
    elif mode == 'production':
        result = verify(['egress-proxy'])
        assert count() == before, 'production diagnostic reached outside recorder'
        result['outside_recorder_production_queries'] = 0
    else:
        raise ValueError('unknown mode')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
