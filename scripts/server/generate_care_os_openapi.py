"""Gera o contrato OpenAPI público para clientes Android.

`firebase_uid` lê o header diretamente para preservar as mensagens 401 atuais;
por isso o esquema Bearer é anotado explicitamente no artefato gerado.
"""
import json
from pathlib import Path

from fastapi import FastAPI

from routers.care_os import router


PUBLIC_PATHS = {
    "/api/public/medical-dashboard/{access_token}",
    "/api/emergency/rescue-card/{emergency_token}",
}


def build_spec():
    app = FastAPI(title="Braga Saúde Care OS Contracts", version="1.1.0")
    app.include_router(router)
    spec = app.openapi()
    spec.setdefault("components", {}).setdefault("securitySchemes", {})["FirebaseBearer"] = {
        "type": "http", "scheme": "bearer", "bearerFormat": "Firebase ID token",
    }
    for path, operations in spec["paths"].items():
        if path not in PUBLIC_PATHS:
            for operation in operations.values():
                if isinstance(operation, dict):
                    operation["security"] = [{"FirebaseBearer": []}]
                    operation.setdefault("responses", {})["401"] = {"description": "Token ausente ou inválido"}
                    operation["responses"]["403"] = {"description": "Vínculo ou permissão insuficiente"}
    return spec


if __name__ == "__main__":
    target = Path(__file__).with_name("openapi_care_os.json")
    target.write_text(json.dumps(build_spec(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OpenAPI Care OS gerada: {target} ({len(build_spec()['paths'])} paths)")
