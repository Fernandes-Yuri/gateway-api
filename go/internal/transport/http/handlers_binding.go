package http

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strings"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
)

// BindingHandler gerencia os endpoints de vínculos familiares e chaves públicas
type BindingHandler struct {
	repo repository.FamilyRepository
}

// NewBindingHandler instancia o handler de vínculos
func NewBindingHandler(repo repository.FamilyRepository) *BindingHandler {
	return &BindingHandler{repo: repo}
}

// HandleFindByCode busca um convite pendente por código de 8 caracteres
func (h *BindingHandler) HandleFindByCode(w http.ResponseWriter, r *http.Request) {
	code := chi.URLParam(r, "code")
	if strings.TrimSpace(code) == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "Código inválido.")
		return
	}

	binding, err := h.repo.FindByConnectionCode(r.Context(), code)
	if err != nil {
		log.Printf("[HandleFindByCode] Falha ao buscar convite por código %q: %v", code, err)
		if errors.Is(err, repository.ErrNotFound) {
			writeJSONError(w, http.StatusNotFound, "Código indisponível ou expirado.")
			return
		}
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Erro interno.")
		return
	}

	writeJSON(w, http.StatusOK, binding)
}

// HandleAcceptInvitation aceita o convite para conectar cuidador ao paciente titular
func (h *BindingHandler) HandleAcceptInvitation(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	var req domain.AcceptInvitationRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Payload JSON inválido.")
		return
	}

	caregiverName := strings.TrimSpace(req.CaregiverName)
	caregiverRelation := strings.TrimSpace(req.CaregiverRelation)
	if caregiverName == "" || caregiverRelation == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "Informe nome e relação com o familiar.")
		return
	}

	var pubKeyPtr *string
	if req.CaregiverPublicKey != nil && strings.TrimSpace(*req.CaregiverPublicKey) != "" {
		validatedKey, err := domain.ValidateE2EEPublicKey(*req.CaregiverPublicKey)
		if err != nil {
			writeJSONError(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		pubKeyPtr = &validatedKey
	}

	binding, err := h.repo.AcceptInvitation(
		r.Context(),
		req.Code,
		uid,
		caregiverName,
		caregiverRelation,
		pubKeyPtr,
	)
	if err != nil {
		log.Printf("[HandleAcceptInvitation] Falha ao aceitar convite %q para uid %s: %v", req.Code, uid, err)
		if errors.Is(err, repository.ErrNotFound) {
			writeJSONError(w, http.StatusNotFound, "Código indisponível ou expirado.")
			return
		}
		if errors.Is(err, repository.ErrConflict) {
			writeJSONError(w, http.StatusConflict, "Código indisponível ou expirado. Peça um novo convite.")
			return
		}
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Falha ao aceitar convite.")
		return
	}

	writeJSON(w, http.StatusOK, binding)
}

// HandlePublishPublicKey cadastra ou atualiza a chave pública ECDH (SPEC-13 §2.1)
func (h *BindingHandler) HandlePublishPublicKey(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	var req domain.PublishFamilyPublicKeyRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Payload JSON inválido.")
		return
	}

	pubKey, err := domain.ValidateE2EEPublicKey(req.PublicKey)
	if err != nil || pubKey == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "Chave pública E2EE obrigatória.")
		return
	}

	if (req.BindingID == nil || strings.TrimSpace(*req.BindingID) == "") &&
		(req.PatientID == nil || strings.TrimSpace(*req.PatientID) == "") {
		writeJSONError(w, http.StatusUnprocessableEntity, "Informe bindingId ou patientId.")
		return
	}

	resp, err := h.repo.PublishPublicKey(r.Context(), req.BindingID, req.PatientID, uid, pubKey)
	if err != nil {
		log.Printf("[HandlePublishPublicKey] Falha ao publicar chave pública para uid %s: %v", uid, err)
		if errors.Is(err, repository.ErrForbidden) {
			writeJSONError(w, http.StatusForbidden, "Vínculo indisponível.")
			return
		}
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Falha ao publicar chave pública.")
		return
	}

	writeJSON(w, http.StatusOK, resp)
}

// HandleListBindings lista todos os vínculos associados ao ID informado
func (h *BindingHandler) HandleListBindings(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	targetUserID := chi.URLParam(r, "userId")
	if targetUserID == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "userId obrigatório.")
		return
	}

	// Regra de segurança: o usuário só pode listar os próprios vínculos
	if uid != targetUserID {
		writeJSONError(w, http.StatusForbidden, "Esta operação pertence a outra conta.")
		return
	}

	bindings, err := h.repo.ListBindings(r.Context(), targetUserID)
	if err != nil {
		log.Printf("[HandleListBindings] Falha ao listar vínculos para %s: %v", targetUserID, err)
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Falha ao listar vínculos.")
		return
	}

	writeJSON(w, http.StatusOK, bindings)
}

// HandleSyncBinding cria (PENDING) ou revoga (REVOKED) um convite familiar.
// Fase #7: porta o contrato local->nuvem que vivia em POST /api/sync/family-binding
// no Python legado, preservando payload, respostas e códigos de erro do app Android.
func (h *BindingHandler) HandleSyncBinding(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	var req domain.SyncFamilyBindingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Payload JSON inválido.")
		return
	}

	req.PatientID = strings.TrimSpace(req.PatientID)
	if req.PatientID == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "Paciente inválido.")
		return
	}

	req.Status = strings.ToUpper(strings.TrimSpace(req.Status))
	if req.Status == "" {
		req.Status = "PENDING"
	}

	switch req.Status {
	case "REVOKED":
		req.ID = strings.TrimSpace(req.ID)
		if _, err := uuid.Parse(req.ID); err != nil {
			writeJSONError(w, http.StatusUnprocessableEntity, "ID inválido.")
			return
		}
	case "PENDING":
		code, err := domain.NormalizeConnectionCode(req.ConnectionCode)
		if err != nil {
			writeJSONError(w, http.StatusUnprocessableEntity, "Código inválido.")
			return
		}
		req.ConnectionCode = code
		if err := domain.ValidateCaregiverRoleAndPermissions(req.CaregiverRole, req.Permissions); err != nil {
			writeJSONError(w, http.StatusUnprocessableEntity, "Papel ou permissões do cuidador inválidos.")
			return
		}
		if req.CaregiverID != nil && strings.TrimSpace(*req.CaregiverID) != "" {
			writeJSONError(w, http.StatusUnprocessableEntity, "Use o convite para conectar o acompanhante.")
			return
		}
		req.CaregiverID = nil
		req.CaregiverName = domain.SanitizeBindingText(req.CaregiverName, 100)
		req.CaregiverRelation = domain.SanitizeBindingText(req.CaregiverRelation, 50)
		if req.PatientPublicKey != nil && strings.TrimSpace(*req.PatientPublicKey) != "" {
			validatedKey, err := domain.ValidateE2EEPublicKey(*req.PatientPublicKey)
			if err != nil {
				writeJSONError(w, http.StatusUnprocessableEntity, err.Error())
				return
			}
			req.PatientPublicKey = &validatedKey
		} else {
			req.PatientPublicKey = nil
		}
	default:
		writeJSONError(w, http.StatusUnprocessableEntity, "Status de vínculo inválido.")
		return
	}

	binding, err := h.repo.SyncBinding(r.Context(), &req, uid)
	if err != nil {
		log.Printf("[HandleSyncBinding] Falha ao sincronizar vínculo %q para uid %s: %v", req.ID, uid, err)
		switch {
		case errors.Is(err, repository.ErrForbidden), errors.Is(err, repository.ErrNotFound):
			// Paridade com o Python legado: vínculo inexistente ou de terceiros -> 403.
			writeJSONError(w, http.StatusForbidden, "Vínculo indisponível.")
		case errors.Is(err, repository.ErrConflict):
			writeJSONError(w, http.StatusConflict, "Convite indisponível. Gere um novo código.")
		case errors.Is(err, repository.ErrDatabaseUnavailable):
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
		default:
			writeJSONError(w, http.StatusInternalServerError, "Falha ao sincronizar vínculo.")
		}
		return
	}

	writeJSON(w, http.StatusOK, domain.SyncFamilyBindingResponse{
		Status: "success",
		ID:     binding.ID,
	})
}
