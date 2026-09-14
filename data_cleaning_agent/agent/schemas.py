"""Public field contract for one flat option observation per input row."""

REQUIRED_FIELDS = frozenset({'timestamps', 'spot', 'strike', 'optiontype', 'expirydate'})

# Normalize spelling only. Do not map received_at to provider/event timestamps.
COLUMN_ALIASES = {
    'timestamps': ('timestamps', 'timestamp', 'timestaps', 'ts', 'event_timestamp'),
    'spot': ('spot', 'spot_price', 'underlying_price'),
    'iv': ('iv', 'implied_volatility'),
    'volume': ('volume', 'option_volume'),
    'oi': ('oi', 'open_interest'),
    'ltp': ('ltp', 'last_price', 'last_traded_price'),
    'strike': ('strike', 'strike_price'),
    'optiontype': ('optiontype', 'option_type', 'right'),
    'expirydate': ('expirydate', 'expiry_date', 'expiry'),
    'underlying': ('underlying', 'underlying_symbol'),
    'symbol': ('symbol', 'option_symbol', 'tradingsymbol', 'trading_symbol'),
    'exchange': ('exchange',),
    'timestamp_source': ('timestamp_source',),
    'provider_timestamp': ('provider_timestamp',),
    'data_status': ('data_status',),
    'source_receipt_id': ('source_receipt_id',),
    'source_freshness': ('source_freshness',),
    'source_version': ('source_version',),
}

DERIVED_INPUT_FIELDS = frozenset({'delta', 'theta', 'gamma', 'vega', 'daystoexpiry', 'days_to_expiry'})
