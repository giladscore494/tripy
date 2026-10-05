"""TRIPY web application (FastAPI): a thin interface over the process-wide RunManager, the run repository, the
loaders and the engine, plus the React production bundle. It imports no UI framework and holds no research logic.

    uvicorn src.api.app:app --host 0.0.0.0 --port "$PORT"      (scripts/start.sh; ONE worker)

    app.py       application factory: lifespan (context, optional MCP), error handlers, routers, the SPA
    frontend.py  the React bundle (frontend/dist): hashed assets, SPA fallback, cache and security headers
    deps.py      ApiContext: paths, RunManager (startup reconciliation), shared controller, vehicle catalog
    auth.py      Bearer TRIPY_ACCESS_TOKEN on /api/* (production; fails closed)
    errors.py    ApiError and the JSON error envelope (never a traceback or a secret)
    service.py   read model of runs (pipeline, failures.explain, loaders, presentation.run_views)
    schemas.py   Pydantic request / response contracts
    routes/      health, runs, config, exports, series, documents, technical
"""
