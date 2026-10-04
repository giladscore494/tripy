"""TRIPY server WITH the read-only MCP endpoint. scripts/start.sh runs this only when TRIPY_MCP_TOKEN is set:

    streamlit run tripy_server.py --server.address=0.0.0.0 --server.port=$PORT ...

`streamlit run` detects the st.App below and serves it with the same uvicorn server and the same Streamlit routes as
`streamlit run app.py`: the dashboard is app.py, unchanged, and one extra route, /mcp/<TRIPY_MCP_TOKEN>, serves the
MCP (src/mcp_server). Without TRIPY_MCP_TOKEN start.sh keeps the plain `streamlit run app.py` command.
"""

import streamlit as st

from src.mcp_server.asgi import mcp_lifespan, mcp_routes

app = st.App("app.py", routes=mcp_routes(), lifespan=mcp_lifespan)
