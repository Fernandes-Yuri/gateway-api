#!/usr/bin/env python3
"""Exporta contrato independente do APK, sem importar startup/segredos do server."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / 'scripts/server'))
from fastapi import FastAPI
from routers.whatsapp_internal import router

app = FastAPI(title='Yuri Atendimento - Gateway WhatsApp',version='1.0.0')
app.include_router(router)
schema = app.openapi()
schema['components']['securitySchemes'] = {
    'IntakeSession': {'type':'http','scheme':'bearer','bearerFormat':'Sessão opaca do atendimento'}}
for path in schema['paths'].values():
    for operation in path.values():
        if isinstance(operation,dict): operation['security'] = [] if operation.get('operationId','').startswith(('enroll_','refresh_')) else [{'IntakeSession':[]}]
output = json.dumps(schema,ensure_ascii=False,indent=2)+'\n'
destination = ROOT / 'docs/contracts/whatsapp-internal-v1.openapi.json'
parser = argparse.ArgumentParser()
parser.add_argument('--check',action='store_true')
args = parser.parse_args()
if args.check:
    if not destination.exists() or destination.read_text()!=output:
        raise SystemExit('OpenAPI desatualizado; execute scripts/export_whatsapp_contract.py')
    print('OpenAPI versionado confere com as rotas.')
else:
    destination.write_text(output)
    print('Contrato OpenAPI exportado.')
