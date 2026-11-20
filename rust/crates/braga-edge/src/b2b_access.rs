//! Authorization before extraction, including unknown B2B routes. Bootstrap lives
//! outside /api/b2b so an unlinked account never enters the business resource tree.
//!
//! Compatibility with the shipped/testlab Android client (governance requirement:
//! the new backend must coexist with the old APK):
//!
//! * The legacy bootstrap aliases (`/api/b2b/contexts`, `contexts/select`,
//!   `account-mode`, `templates`, `enrollments/join`) share handlers with the
//!   canonical `/api/session/*` and `/api/programs/*` endpoints. They only require
//!   authentication because each handler enforces its own ownership/eligibility
//!   rules (contexts are scoped to the caller's uid, join validates the invite and
//!   the caller has no enrollment yet).
//! * `/api/b2b/invite/:code` is a public capability URL: the legacy client calls it
//!   before login (`AuthInterceptor` omits the bearer token for it) and knowing the
//!   code is the capability. The canonical `/api/programs/invite/:code` is outside
//!   the guard entirely and stays public as well.
//! * Self-scoped bootstrap (authenticated, no ownership needed): the exact path
//!   `/api/b2b/program/me` (GET/POST/PUT), `/api/b2b/templates` (static catalog)
//!   and `/api/business/registration` (+ `/templates`). Handlers only read/create
//!   the caller's own program, and `save_my_program_handler` promotes
//!   `PERSONAL -> BUSINESS_ONLY` when the first program is created. The legacy app
//!   creates its first program through `/api/b2b/program/me` while `owner = false`.
//!   Every other `/api/b2b/program/me/*` subpath remains `business && owner`
//!   (exact-path match, never prefix, so subpaths are not opened).
//! * A legacy owner whose `account_mode` is still `PERSONAL` is handled by
//!   migration 093 (backfill to `DUAL`).
use std::sync::Arc;

use axum::{
    extract::{Request, State},
    http::StatusCode,
    middleware::Next,
    response::Response,
};
use sqlx::Row;

use crate::{
    authenticate_request,
    b2b::{json_error, require_pool},
    AppState,
};

pub async fn guard(State(state): State<Arc<AppState>>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    if is_public(path) {
        return next.run(request).await;
    }
    let session_only = is_session_only(path);
    if !session_only && !path.starts_with("/api/b2b/") {
        return next.run(request).await;
    }
    let identity = match authenticate_request(
        request.headers(),
        &state,
        request.method().as_str(),
        path,
    )
    .await
    {
        Ok(identity) => identity,
        Err(response) => return response,
    };
    if session_only {
        return next.run(request).await;
    }
    let pool = match require_pool(&state) {
        Ok(pool) => pool,
        Err((code, message)) => return json_error(code, message),
    };
    let access = match sqlx::query(include_str!("sql/b2b_access.sql"))
        .bind(&identity.uid)
        .fetch_one(pool)
        .await
    {
        Ok(row) => row,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao validar acesso ao programa.",
            )
        }
    };
    let business: bool = access.get("business_account");
    let owner: bool = access.get("owner");
    let participant: bool = access.get("participant");
    let family: bool = access.get("family");
    let allowed = allowed(path, business, owner, participant, family);
    if !allowed {
        return json_error(
            StatusCode::FORBIDDEN,
            "Esta conta não tem acesso ativo a este recurso.",
        );
    }
    next.run(request).await
}

/// Public capability URLs. `/api/programs/invite/` never reaches the guard (it is
/// not one of the guarded prefixes); both are listed for explicit intent.
fn is_public(path: &str) -> bool {
    path.starts_with("/api/b2b/invite/") || path.starts_with("/api/programs/invite/")
}

/// Bootstrap endpoints that require a valid session but no program ownership or
/// enrollment: handlers only act on the caller's own identity/program. Includes
/// the canonical `/api/session/*`/`/api/programs/join` routes and the legacy
/// aliases still used by the shipped/testlab Android client.
fn is_session_only(path: &str) -> bool {
    path.starts_with("/api/session/")
        || path == "/api/programs/join"
        || matches!(
            path,
            "/api/b2b/contexts"
                | "/api/b2b/contexts/select"
                | "/api/b2b/account-mode"
                | "/api/b2b/enrollments/join"
                // Path EXATO (nunca prefixo): o app antigo cria o 1o programa aqui
                // com owner=false; os subcaminhos seguem business && owner.
                | "/api/b2b/program/me"
                | "/api/b2b/templates"
        )
        || path.starts_with("/api/business/registration")
}

fn allowed(path: &str, business: bool, owner: bool, participant: bool, family: bool) -> bool {
    if path.starts_with("/api/b2b/program/") {
        business && owner
    } else if path.starts_with("/api/b2b/enrollments/") {
        participant
    } else if path.starts_with("/api/b2b/family/") {
        family
    } else {
        (business && owner) || participant || family
    }
}

#[cfg(test)]
mod tests {
    use super::{allowed, is_public, is_session_only};

    #[test]
    fn legacy_bootstrap_aliases_only_require_authentication() {
        for path in [
            "/api/session/contexts",
            "/api/session/context",
            "/api/session/account-mode",
            "/api/programs/join",
            "/api/b2b/contexts",
            "/api/b2b/contexts/select",
            "/api/b2b/account-mode",
            "/api/b2b/enrollments/join",
            "/api/b2b/templates",
            "/api/b2b/program/me",
            "/api/business/registration",
            "/api/business/registration/templates",
        ] {
            assert!(is_session_only(path), "bootstrap esperado: {path}");
            assert!(!is_public(path), "bootstrap exige autenticacao: {path}");
        }
        // Aderir por convite nao pode exigir vinculo previo (usuario ainda nao e
        // participante). O handler valida o codigo e a elegibilidade.
        assert!(is_session_only("/api/b2b/enrollments/join"));
        assert!(!is_session_only("/api/b2b/program/me/participants"));
        assert!(!is_public("/api/b2b/contexts"));
    }

    #[test]
    fn invite_previews_are_public_capability_routes() {
        // App legado consulta o convite antes de autenticar.
        assert!(is_public("/api/b2b/invite/CLINA-1111"));
        assert!(is_public("/api/programs/invite/CLINA-1111"));
        assert!(!is_session_only("/api/b2b/invite/CLINA-1111"));
    }

    #[test]
    fn program_me_exact_path_is_bootstrap_but_subpaths_stay_management() {
        // Lado bootstrap: path EXATO `/api/b2b/program/me` (nunca prefixo). E o
        // fluxo do app antigo criando o 1o programa com owner=false.
        assert!(is_session_only("/api/b2b/program/me"));
        assert!(is_session_only("/api/b2b/templates"));
        // "/api/b2b/program/me/" (barra final) nao e o path exato.
        assert!(!is_session_only("/api/b2b/program/me/"));
        // Lado gestao: TODO subcaminho continua business && owner.
        for path in [
            "/api/b2b/program/me/participants",
            "/api/b2b/program/me/participants/patient-1/summary",
            "/api/b2b/program/me/participants/patient-1/remove",
            "/api/b2b/program/me/participants/patient-1/messages",
            "/api/b2b/program/me/participants/patient-1/chat-handoff",
            "/api/b2b/program/me/reminders",
            "/api/b2b/program/me/appointments",
            "/api/b2b/program/me/experience",
            "/api/b2b/program/me/close",
            "/api/b2b/program/me/submit",
            "/api/b2b/program/me/proofs",
            "/api/b2b/program/me/logo",
            "/api/b2b/program/me/invite-policy",
            "/api/b2b/program/me/invite-code/regenerate",
        ] {
            assert!(
                !is_session_only(path),
                "subcaminho de gestao nao e bootstrap: {path}"
            );
            assert!(!is_public(path), "subcaminho nunca e publico: {path}");
            assert!(
                allowed(path, true, true, false, false),
                "owner business acessa gestao: {path}"
            );
            assert!(
                !allowed(path, true, false, false, false),
                "business sem programa nao acessa gestao: {path}"
            );
            assert!(
                !allowed(path, false, true, false, false),
                "owner sem business nao acessa gestao: {path}"
            );
            assert!(!allowed(path, false, false, false, false));
        }
    }

    #[test]
    fn registration_is_self_scoped_bootstrap() {
        for path in [
            "/api/business/registration",
            "/api/business/registration/templates",
        ] {
            assert!(is_session_only(path), "cadastro e self-scoped: {path}");
            assert!(!is_public(path), "cadastro exige autenticacao: {path}");
        }
    }

    #[test]
    fn management_routes_still_require_active_link() {
        for path in [
            "/api/b2b/program/me/participants",
            "/api/b2b/program/me/participants/patient-1/summary",
            "/api/b2b/program/me/reminders",
            "/api/b2b/program/me/appointments",
            "/api/b2b/program/me/experience",
            "/api/b2b/program/me/logo",
            "/api/b2b/program/me/proofs",
            "/api/b2b/program/me/invite-policy",
            "/api/b2b/enrollments/00000000-0000-0000-0000-000000000000/messages",
            "/api/b2b/family/patients/patient-1/program-overview",
        ] {
            assert!(!is_public(path), "rota de gestao nunca e publica: {path}");
            assert!(
                !is_session_only(path),
                "rota de gestao exige vinculo: {path}"
            );
            assert!(
                !allowed(path, false, false, false, false),
                "rota de gestao exige vinculo: {path}"
            );
        }
    }

    #[test]
    fn ordinary_and_family_accounts_cannot_enter_management() {
        for path in [
            "/api/b2b/program/me/participants",
            "/api/b2b/program/me/participants/other/summary",
            "/api/b2b/program/me/experience",
        ] {
            assert!(!allowed(path, false, false, false, false));
            assert!(!allowed(path, false, false, true, true));
            assert!(!allowed(path, false, true, false, false));
            assert!(!allowed(path, true, false, false, false));
            assert!(allowed(path, true, true, false, false));
        }
        assert!(allowed(
            "/api/b2b/family/patients/p/program-overview",
            false,
            false,
            false,
            true
        ));
        assert!(!allowed(
            "/api/b2b/enrollments/p/messages",
            false,
            false,
            false,
            true
        ));
        assert!(!allowed("/api/b2b/unknown", false, false, false, false));
    }
}
