"""Feed Comunitario — Publicacoes de Orgulho Familiar (PWA Cuidador, doc 02/01 §4).

Postagens têm visibilidade 'community' (toda a comunidade Braga Saude) ou
'family' (restrita ao Circulo Familiar: titular + cuidadores com vinculo
ACTIVE). A escrita exige vinculo familiar com o patient_id — o cuidador
so publica sobre o proprio familiar. Leitura é sempre escopada ao usuario
autenticado: comunidade inteira + posts privados dos seus vinculos.
"""
from types import SimpleNamespace
from typing import Literal, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api_identity import firebase_uid, require_family_access
from security_defense import sanitize_zero_trust_text

router = APIRouter(tags=["Comunidade"])
_state = SimpleNamespace(db_pool=None)

MAX_POSTS = 100


def configure_state(*, db_pool=None):
    _state.db_pool = db_pool


def _pool():
    if not _state.db_pool:
        raise HTTPException(503, "Banco indisponível.")
    return _state.db_pool


class CommunityPostCreate(BaseModel):
    patientId: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=500)
    type: Literal["achievement", "recipe", "walk", "milestone", "tip"] = "achievement"
    visibility: Literal["community", "family"] = "community"
    authorName: Optional[str] = Field(default=None, max_length=100)


def _epoch_ms(value) -> Optional[int]:
    return int(value.timestamp() * 1000) if value else None


def _serialize(row, liked: bool) -> dict:
    return {
        "id": str(row["id"]),
        "patientId": row["patient_id"],
        "authorName": row["author_name"],
        "authorUserId": row["author_user_id"],
        "authorRelation": row["author_relation"],
        "content": row["content"],
        "type": row["type"],
        "visibility": row["visibility"],
        "createdAt": _epoch_ms(row["created_at"]),
        "expiresAt": _epoch_ms(row["expires_at"]),
        "likes": row["likes_count"],
        "isLikedByViewer": liked,
    }


@router.get("/api/community/posts")
@router.get("/api/community/posts/{patient_id}")
async def list_community_posts(patient_id: str = "", uid: str = Depends(firebase_uid)):
    """Comunidade inteira (visibility='community') + posts privados do
    Circulo Familiar do usuario (titular ou cuidador ACTIVE de qualquer
    vinculo). O patient_id do caminho e opcional: o escopo privado vem dos
    vinculos reais no banco, nunca do parametro — nunca vaza post alheio."""
    async with _pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.*,
                   EXISTS(SELECT 1 FROM community_post_likes l
                          WHERE l.post_id = p.id AND l.user_id = $1) AS liked
            FROM community_posts p
            WHERE p.visibility = 'community'
               OR (
                    p.visibility = 'family'
                    AND (
                        p.patient_id = $1
                        OR EXISTS (
                            SELECT 1 FROM family_bindings b
                            WHERE b.patient_id = p.patient_id
                              AND b.caregiver_id = $1
                              AND b.status = 'ACTIVE'
                        )
                    )
               )
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT $2
            """,
            uid, MAX_POSTS,
        )
    return [_serialize(row, bool(row["liked"])) for row in rows]


@router.post("/api/community/posts", status_code=201)
async def create_community_post(payload: CommunityPostCreate, uid: str = Depends(firebase_uid)):
    """Publica no feed. Exige vinculo familiar com o patient_id (§4: o
    cuidador so celebra o propio familiar)."""
    clean_content = sanitize_zero_trust_text(payload.content, max_length=500, field_name="content")
    if not clean_content.strip():
        raise HTTPException(422, "Escreva a publicação antes de enviar.")
    clean_author = sanitize_zero_trust_text(payload.authorName, max_length=100, field_name="authorName")

    async with _pool().acquire() as conn:
        await require_family_access(conn, uid, payload.patientId)
        binding = await conn.fetchrow(
            "SELECT caregiver_name, caregiver_relation FROM family_bindings "
            "WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE'",
            payload.patientId, uid,
        )
        if binding:
            author_name = clean_author or binding["caregiver_name"] or "Familiar"
            author_relation = binding["caregiver_relation"] or ""
        else:
            # require_family_access so deixa passar o proprio titular aqui.
            profile_name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid)
            author_name = clean_author or profile_name or "Titular"
            author_relation = "Titular"
        row = await conn.fetchrow(
            """INSERT INTO community_posts
               (id, patient_id, author_user_id, author_name, author_relation,
                content, type, visibility, created_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,now())
               RETURNING *""",
            uuid.uuid4(), payload.patientId, uid, author_name, author_relation,
            clean_content, payload.type, payload.visibility,
        )
    return _serialize(row, False)


@router.post("/api/community/posts/{post_id}/like")
async def like_community_post(post_id: str, uid: str = Depends(firebase_uid)):
    """Curtida idempotente: cada usuario conta uma unica vez por publicacao."""
    try:
        pid = uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(422, "Publicação inválida.")
    async with _pool().acquire() as conn, conn.transaction():
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM community_posts WHERE id=$1)", pid)
        if not exists:
            raise HTTPException(404, "Publicação não encontrada.")
        inserted = await conn.execute(
            "INSERT INTO community_post_likes (post_id, user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
            pid, uid,
        )
        if inserted == "INSERT 0 1":
            await conn.execute(
                "UPDATE community_posts SET likes_count = likes_count + 1 WHERE id=$1", pid)
        likes = await conn.fetchval("SELECT likes_count FROM community_posts WHERE id=$1", pid)
    return {"success": True, "likes": likes}
