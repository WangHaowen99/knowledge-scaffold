"""Execute the actual narrow route with synthetic ACL and persistence adapters."""
import ast
import asyncio
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from fastapi import Depends
import pytest


def test_actual_route_cannot_spoof_owner_and_is_idempotent(monkeypatch):
    path=Path(__file__).resolve().parents[2]/'consumer/backend/api/routes/workspaces.py'
    tree=ast.parse(path.read_text())
    route=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='ensure_evaluation_workspace_api')
    route.decorator_list=[]
    module=ast.Module(body=[route],type_ignores=[])
    class Request: pass
    class AuthUser: pass
    records={}
    async def read(request): return request.payload
    async def pool(request,kind,func,*args,**kwargs): return func(*args,**kwargs)
    def provision(auth, *, owner, run_id):
        import hashlib
        rid='eval-'+hashlib.sha256((owner+':'+run_id).encode()).hexdigest()[:32]
        created=rid not in records
        records.setdefault(rid,{'id':rid,'owner':owner})
        return {'created':created,'workspace':records[rid]}
    fake=ModuleType('api.auth.service_auth')
    def authorized(request): return request.service_valid
    fake.evaluation_service_authorized=authorized
    monkeypatch.setitem(sys.modules,'api.auth.service_auth',fake)
    from fastapi.responses import JSONResponse
    import re
    scope=dict(Request=Request,AuthUser=AuthUser,Depends=Depends,require_authenticated_user=lambda:None,
        _request_json_object=read,_workspace_forbidden=lambda:JSONResponse({},status_code=403),
        run_in_runtime_pool=pool,provision_evaluation_workspace=provision,
        get_auth_manager=lambda request:None,WorkspaceAuthorizationError=PermissionError,
        _WORKSPACE_IDENTITY_ERRORS=(),JSONResponse=JSONResponse,re=re)
    exec(compile(module,str(path),'exec'),scope)
    invoke=scope[route.name]
    request=SimpleNamespace(payload={'run_id':'a'*32},service_valid=True)
    user=SimpleNamespace(username='operator_a')
    first=asyncio.run(invoke(request,user))
    assert first['created'] and first['workspace']['owner']=='operator_a'
    second=asyncio.run(invoke(request,user))
    assert not second['created'] and first['workspace']==second['workspace']
    request.payload['owner']='operator_b'
    assert asyncio.run(invoke(request,user)).status_code==422
    request.payload={'run_id':'a'*32};request.service_valid=False
    assert asyncio.run(invoke(request,user)).status_code==403
