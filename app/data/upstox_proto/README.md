# Upstox Market Data Feed — official protobuf schema (V3)

- Source: https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto
- Downloaded: 2026-09-21, kept byte-identical (no edits).
- sha256(MarketDataFeed.proto): 3e1c939dce2c83a3fef91405c5ca268dfa089aae973319f56685f12a5afcf612
- Bindings: `MarketDataFeed_pb2.py` generated with grpcio-tools ~=1.68 (protoc
  from the protobuf 5.29 line):
  `python -m grpc_tools.protoc --proto_path=app/data/upstox_proto --python_out=app/data/upstox_proto app/data/upstox_proto/MarketDataFeed.proto`
- Runtime requirement: `protobuf>=5.29,<7` (already in requirements.txt).
- Documentation: https://upstox.com/developer/api-documentation/get-market-data-feed-authorize-v3/
  and https://upstox.com/developer/api-documentation/v3/get-market-data-feed/

This schema is used ONLY to decode read-only market data frames.
