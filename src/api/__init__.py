"""TRIPY HTTP API (FastAPI): a thin interface over the same RunManager, run repository, loaders and engine the
Streamlit dashboard uses. It never imports a Streamlit module and holds no research logic of its own.

    uvicorn src.api.app:app --host 0.0.0.0 --port 8000

    app.py      application factory: lifespan (context, optional MCP), error handlers, routers
    deps.py     ApiContext: paths, RunManager (startup reconciliation), shared controller, vehicle catalog
    auth.py     Bearer TRIPY_ACCESS_TOKEN on /api/* (production; same rule as the dashboard's lock screen)
    errors.py   ApiError and the JSON error envelope (never a traceback or a secret)
    service.py  read model of runs built from the dashboard's own functions (pipeline, explain, loaders)
    schemas.py  Pydantic request / response contracts
    routes/     health, runs, config, exports
"""
