"""Authoritative small correctness setup and explicit opt-in HTTP qualification."""
import os
import json
import platform
import statistics
import time

import pytest

from tests.test_discovery_review_list import production as production
from tests.test_discovery_review_index import add_population, drive, manifest, service_for, page_link, tokens


def benchmark_population(production, population, record_property=None):
    client, bench, matter = production
    begin = time.perf_counter()
    store = bench.source_store(matter)
    add_population(store, population - len(store.documents))
    expected = set(store.documents)
    registered = time.perf_counter()
    first = drive(production, manifest(population), params={'view': 'fields'})
    assert len(expected) == population
    assert f'Indexed {population} of {population}; 0 pending; 0 errors.' in first.text
    service = service_for(bench)
    initial = service.repository.state(matter.matter_id)
    generation = initial['generation']
    built = time.perf_counter()
    # Reenter the application's real source-store opening and catalog reconciliation path.
    store.close()
    bench._stores.pop(matter.matter_id)
    reopened = bench.source_store(matter)
    assert set(reopened.documents) == expected
    page = drive(production, manifest(population), params={'view': 'fields'})
    assert service.repository.state(matter.matter_id)['generation'] != generation
    projected = service.repository.rows('SELECT document_id FROM workbench_discovery_index_item WHERE matter_id=?', (matter.matter_id,))
    assert {row[0] for row in projected} == expected
    assert f'Indexed {population} of {population}; 0 pending; 0 errors.' in page.text
    assert 'while indexing' not in page.text
    if record_property is not None:
        record_property('registry_setup_seconds', registered - begin)
        record_property('initial_build_seconds', built - registered)
        record_property('reopen_and_reconciliation_seconds', time.perf_counter() - built)
        for label, state in (('initial', initial), ('reconciled', service.repository.state(matter.matter_id))):
            cursor = json.loads(state['checkpoint'])
            record_property(label + '_committed_steps', cursor['sequence'])
            record_property(label + '_work_credits', cursor['accumulators']['work'])
            record_property(label + '_early_yields', cursor['accumulators']['early'])
        record_property('workspace_storage_bytes', sum(path.stat().st_size for path in bench.workspace.path.parent.glob(bench.workspace.path.name + '*') if path.is_file()))
        record_property('value_rows', service.repository.rows('SELECT COUNT(*) FROM workbench_discovery_index_value WHERE matter_id=?', (matter.matter_id,))[0][0])
    return page


def test_benchmark_population_survives_reconciliation(production):
    page = benchmark_population(production, 201)
    client, bench, matter = production
    found = tokens(page.text)
    while link := page_link(page.text, 'next'):
        page = client.get(link)
        found.extend(tokens(page.text))
    assert len(found) == len(set(found)) == 201


@pytest.mark.discovery_index_perf
@pytest.mark.timeout(86400)
@pytest.mark.skipif(os.getenv('EXCULPATA_RUN_DISCOVERY_INDEX_PERF') != '1', reason='explicit million-item qualification only')
def test_million_item_page_latency(production, record_property):
    import hashlib
    import sqlite3
    import resource
    page = benchmark_population(production, 1_000_000, record_property)
    client, bench, matter = production
    record_property('python', platform.python_version())
    record_property('sqlite', sqlite3.sqlite_version)
    record_property('platform', platform.platform())
    record_property('logical_cpus', os.cpu_count())
    record_property('physical_memory_bytes', os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES'))
    record_property('peak_rss_kib', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    record_property('warmup_windows', 100)
    store = bench.source_store(matter)
    identities = sorted(store.documents)
    record_property('dataset_identity_digest', hashlib.sha256('\n'.join(identities).encode()).hexdigest())
    for _ in range(100):
        page = client.get(page_link(page.text, 'next'))
        assert page.status_code == 200
    deep_next, previous = page_link(page.text, 'next'), page_link(page.text, 'previous')
    for name, url in (('first', f'/matters/{matter.slug}/production?view=fields'), ('deep_next', deep_next), ('previous', previous)):
        elapsed = []
        for _ in range(100):
            begin = time.perf_counter()
            response = client.get(url)
            elapsed.append(time.perf_counter() - begin)
            assert response.status_code == 200 and len(tokens(response.text)) == 100
        p95 = statistics.quantiles(elapsed, n=100)[94]
        record_property(name + '_p95_seconds', p95)
        assert p95 < 0.300

    for needle in ('record-987654.txt', 'record-', '%_'):
        elapsed = []
        for _ in range(100):
            begin = time.perf_counter()
            response = client.get(f'/matters/{matter.slug}/production', params={'q': needle, 'view': 'fields'})
            elapsed.append(time.perf_counter() - begin)
            assert response.status_code == 200 and 'Filter covers 100 of 1000000 items' in response.text
        record_property('substring_' + needle + '_p95_seconds', statistics.quantiles(elapsed, n=100)[94])
    begin = time.perf_counter()
    response = client.get(f'/matters/{matter.slug}/production', params={'q': '%_', 'view': 'fields'})
    windows = 1
    while link := page_link(response.text, 'next'):
        response = client.get(link)
        assert response.status_code == 200
        windows += 1
    assert windows == 10_000 and 'Filter covers 1000000 of 1000000 items' in response.text
    record_property('exhaustive_filter_seconds', time.perf_counter() - begin)
