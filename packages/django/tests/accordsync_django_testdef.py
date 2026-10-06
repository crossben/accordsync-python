from accordsync_core import define_schema, lww
from accordsync_server import Access, Auth, Limits, define_server

definition = define_server(
    schema=define_schema({"note": {"title": lww()}}),
    scopes={"note": lambda r: ["all"]},
    access=lambda c: Access(read=["all"], write=["all"]),
    auth=Auth.hs256("x" * 32),
    limits=Limits(max_body_bytes=100),
)
not_a_definition = 42
