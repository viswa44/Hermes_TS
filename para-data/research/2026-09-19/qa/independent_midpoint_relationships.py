"""Descriptive, receipt-time aligned one-minute changes; no predictive claims."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

ROOT = Path('/Users/viswatej/Desktop/openalgo')
OUT = Path(__file__).resolve().parent
REPORT = ROOT / 'data_cleaning_agent/runtime/enrichment_verification_2026-09-19.json'


def main():
    report = json.loads(REPORT.read_text())
    frames, checked = [], []
    for item in report['verified']:
        tables = {}
        for stem in ('observations', 'options'):
            filename = stem + '.parquet'
            path = Path(item['run_dir']) / filename
            expected = item['artifact_checks'][filename]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == expected['sha256'] and path.stat().st_size == expected['size_bytes'], path
            checked.append({'date': item['date'], 'file': str(path), 'sha256': digest})
            tables[stem] = pd.read_parquet(path)
        obs, opt = tables['observations'], tables['options']
        assert len(obs) == len(opt) == item['rows_per_table']
        merged = obs[['observation_id', 'timestamps', 'trading_date', 'spot', 'timestamp_source', 'source_freshness']].merge(
            opt[['observation_id', 'contract_key', 'symbol', 'optiontype', 'bid', 'ask', 'ltp']],
            on='observation_id', validate='one_to_one')
        assert len(merged) == len(obs)
        assert merged['timestamps'].dt.tz_convert('Asia/Kolkata').dt.date.astype(str).eq(item['date']).all()
        merged['midpoint'] = (merged['bid'] + merged['ask']) / 2
        assert merged[['spot','bid','ask','midpoint']].notna().all().all()
        assert merged['ask'].ge(merged['bid']).all()
        frames.append(merged)
    data = pd.concat(frames, ignore_index=True).sort_values(['trading_date','optiontype','timestamps'])
    windows, filters = [], []
    for (day, side), quotes in data.groupby(['trading_date','optiontype'], sort=True):
        quotes = quotes.sort_values('timestamps').reset_index(drop=True)
        gaps = quotes['timestamps'].diff().dt.total_seconds()
        contract_switch = quotes['contract_key'].ne(quotes['contract_key'].shift()).fillna(True)
        quotes['segment'] = (gaps.gt(10) | contract_switch).cumsum()
        ns = quotes['timestamps'].astype('int64').to_numpy()
        minute_grid = pd.date_range(quotes['timestamps'].min().ceil('min'),
                                    quotes['timestamps'].max().ceil('min'), freq='min')
        last_end = None
        counts = {'date':day, 'optiontype':side, 'source_rows':len(quotes),
                  'segments':int(quotes['segment'].nunique()), 'clock_endpoints':len(minute_grid),
                  'end_too_old':0, 'no_start_within_65s':0, 'segment_changed':0,
                  'overlap_excluded':0, 'selected':0}
        for grid in minute_grid:
            end_index = int(np.searchsorted(ns, grid.value, side='right') - 1)
            if end_index < 0 or (grid.value - ns[end_index])/1e9 > 5:
                counts['end_too_old'] += 1
                continue
            end = quotes.iloc[end_index]
            target = ns[end_index] - 60_000_000_000
            start_index = int(np.searchsorted(ns, target, side='right') - 1)
            if start_index < 0 or (ns[end_index]-ns[start_index])/1e9 > 65:
                counts['no_start_within_65s'] += 1
                continue
            start = quotes.iloc[start_index]
            if start['segment'] != end['segment']:
                counts['segment_changed'] += 1
                continue
            if last_end is not None and start['timestamps'] < last_end:
                counts['overlap_excluded'] += 1
                continue
            last_end = end['timestamps']
            seconds = (end['timestamps']-start['timestamps']).total_seconds()
            assert 60 <= seconds <= 65
            assert start['contract_key'] == end['contract_key']
            assert gaps.iloc[start_index+1:end_index+1].le(10).all()
            windows.append({'date':day, 'optiontype':side, 'clock_endpoint':grid.isoformat(),
                'start':start['timestamps'].isoformat(), 'end':end['timestamps'].isoformat(),
                'seconds':seconds, 'contract_key':end['contract_key'],
                'start_id':start['observation_id'], 'end_id':end['observation_id'],
                'spot_change':float(end['spot']-start['spot']),
                'midpoint_change':float(end['midpoint']-start['midpoint']),
                'ltp_change':float(end['ltp']-start['ltp']),
                'start_spread':float(start['ask']-start['bid']),
                'end_spread':float(end['ask']-end['bid'])})
            counts['selected'] += 1
        filters.append(counts)
    samples = pd.DataFrame(windows)
    summaries = []
    for group_fields in (['date','optiontype'], ['optiontype']):
        for group, frame in samples.groupby(group_fields):
            group = group if isinstance(group, tuple) else (group,)
            names = dict(zip(group_fields, group))
            summaries.append({'date':names.get('date','POOLED'), 'optiontype':names['optiontype'],
                'n':len(frame), 'pearson':float(frame['spot_change'].corr(frame['midpoint_change'])),
                'spearman':float(frame['spot_change'].rank().corr(frame['midpoint_change'].rank())),
                'ltp_pearson':float(frame['spot_change'].corr(frame['ltp_change'])),
                'seconds_median':float(frame['seconds'].median()),
                'seconds_min':float(frame['seconds'].min()), 'seconds_max':float(frame['seconds'].max()),
                'zero_spot_change':int(frame['spot_change'].eq(0).sum()),
                'zero_midpoint_change':int(frame['midpoint_change'].eq(0).sum()),
                'unique_contracts':int(frame['contract_key'].nunique())})
    summary = {'source_report':str(REPORT), 'source_rows':len(data), 'verified_files':checked,
        'method': 'Contemporaneous spot point change versus same-contract bid/ask midpoint point change. Last quote at or before each IST minute boundary, no older than 5 seconds. Start is last quote at or before end minus 60 seconds, at most 65 seconds old. Segments break on date, contract switch, or successive quote gap greater than 10 seconds. Greedy selection rejects intervals overlapping a previously selected interval for the same day and option side.',
        'sample_windows':len(samples), 'stats':summaries, 'filter_counts':filters,
        'limitations': ['Descriptive contemporaneous association, not prediction or causality.',
            'Only nine trading days, several incomplete; no significance or independence claim.',
            'ATM contract switching excludes transition windows and selects persistent contracts.',
            'Stored timestamps are application receipt time or unverified legacy time, not verified exchange event time.',
            'Midpoint is a quote-based summary, not an executable or last-traded price.',
            'CE and PE share underlying observations; do not treat their pooled counts as independent.',
            'Nonoverlapping windows remain temporally dependent; minute-clock selection and 60–65 second matching can reject endpoints due to receipt jitter.']}
    samples.to_csv(OUT/'independent_one_minute_samples.csv',index=False)
    pd.DataFrame(summaries).to_csv(OUT/'independent_correlations.csv',index=False)
    (OUT/'independent_relationship_report.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(pd.DataFrame(summaries).to_string(index=False))
    print(json.dumps({'source_rows':len(data),'hashes_verified':len(checked),'sample_windows':len(samples),'output':str(OUT)},indent=2))


if __name__ == '__main__':
    main()
