"""API de referências clínicas oficiais compartilhada por todos os canais."""

from typing import Optional

from fastapi import Depends, HTTPException, Query, status

from api_identity import firebase_uid


def _serialize(row):
    item = dict(row)
    if item.get("updated_at"):
        item["updated_at"] = item["updated_at"].isoformat()
    return item


def install_clinical_reference_routes(app, get_pool):
    def pool():
        current = get_pool()
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Banco de dados indisponível no momento.",
            )
        return current

    @app.get("/api/clinical/references")
    async def list_clinical_references(
        category: Optional[str] = Query(None, min_length=1, max_length=100),
        item_key: Optional[str] = Query(None, min_length=1, max_length=100),
        _uid: str = Depends(firebase_uid),
    ):
        conditions = []
        arguments = []
        if category:
            arguments.append(category)
            conditions.append(f"category = ${len(arguments)}")
        if item_key:
            arguments.append(item_key)
            conditions.append(f"item_key = ${len(arguments)}")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT item_key, item_name, category, unit, min_target, max_target,
                   min_critical, max_critical, interpretation_hint, institution,
                   document_version, parameter, updated_at
              FROM clinical_references
              {where}
             ORDER BY category, item_name
        """
        async with pool().acquire() as conn:
            rows = await conn.fetch(query, *arguments)
        return {"references": [_serialize(row) for row in rows], "count": len(rows)}
