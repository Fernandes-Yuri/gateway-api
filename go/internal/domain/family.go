package domain

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"time"
)

const (
	E2EECurrentVersion     = 1
	E2EEGCMIVBytes         = 12
	E2EEGCMTagBytes        = 16
	E2EEMaxCiphertextBytes = 8192
	E2EEPushBody           = "Nova mensagem protegida por criptografia ponta a ponta."

	JokerConnectionCode = "TESTFAM1"
	JokerPatientUID     = "patient-demo-testfam"
	JokerPatientName    = "Carlos Braga (Paciente Teste)"
)

var (
	ErrInvalidE2EEKey         = errors.New("chave pública E2EE inválida")
	ErrInvalidE2EEKeySize     = errors.New("tamanho de chave pública E2EE inválido")
	ErrMissingE2EEPayload     = errors.New("payload E2EE ausente")
	ErrEmptyE2EEPayload       = errors.New("payload E2EE vazio")
	ErrMalformedE2EEPayload   = errors.New("payload E2EE malformado")
	ErrInvalidE2EEJSON        = errors.New("JSON de payload E2EE inválido")
	ErrUnsupportedE2EEVersion = errors.New("versão de criptografia E2EE não suportada")

	ErrInvalidSentAt  = errors.New("horário de envio inválido")
	ErrFutureSentAt   = errors.New("confira a data e a hora do aparelho")
	ErrExpiredMessage = errors.New("mensagem expirada")

	ErrInvalidConnectionCode = errors.New("código de convite inválido")
	ErrInvalidCaregiverRole  = errors.New("papel de cuidador inválido")
	ErrInvalidPermissions    = errors.New("permissões do cuidador inválidas")
	ErrInvalidBindingStatus  = errors.New("status de vínculo inválido")
)

// FamilyMessage representa a entidade persistida no PostgreSQL (tabela family_messages)
type FamilyMessage struct {
	ID                string     `json:"id" db:"id"`
	PatientID         string     `json:"patientId" db:"patient_id"`
	SenderID          string     `json:"senderId" db:"sender_id"`
	SenderName        string     `json:"senderName" db:"sender_name"`
	MessageText       string     `json:"messageText" db:"message_text"`
	BodyEncrypted     *string    `json:"bodyEncrypted" db:"body_encrypted"`
	EncryptionVersion int16      `json:"encryptionVersion" db:"encryption_version"`
	IconType          string     `json:"iconType" db:"icon_type"`
	IsRead            bool       `json:"isRead" db:"is_read"`
	SentAt            time.Time  `json:"sentAt" db:"sent_at"`
	ExpiresAt         time.Time  `json:"expiresAt" db:"expires_at"`
	DeletedAt         *time.Time `json:"deletedAt" db:"deleted_at"`
}

// SendFamilyMessageRequest é o payload JSON recebido do Android
type SendFamilyMessageRequest struct {
	ID                string  `json:"id"`
	PatientID         string  `json:"patientId"`
	SenderID          string  `json:"senderId"`
	SenderName        string  `json:"senderName"`
	MessageText       string  `json:"messageText"`
	BodyEncrypted     *string `json:"bodyEncrypted,omitempty"`
	EncryptionVersion *int16  `json:"encryptionVersion,omitempty"`
	IconType          string  `json:"iconType"`
	SentAt            string  `json:"sentAt"`
}

// SendFamilyMessageResponse é a resposta HTTP 200 para o envio
type SendFamilyMessageResponse struct {
	Status            string `json:"status"`
	ID                string `json:"id"`
	EncryptionVersion int16  `json:"encryptionVersion"`
}

// MarkMessagesReadRequest é o payload JSON para marcar mensagens de uma conversa como lidas
type MarkMessagesReadRequest struct {
	PatientID  string   `json:"patientId"`
	MessageIDs []string `json:"messageIds"`
}

// DeleteFamilyMessageRequest é o payload para exclusão (tombstone)
type DeleteFamilyMessageRequest struct {
	ID        string  `json:"id"`
	PatientID *string `json:"patientId,omitempty"`
	SentAt    *string `json:"sentAt,omitempty"`
}

// PublishFamilyPublicKeyRequest payload para cadastro de chave pública ECDH
type PublishFamilyPublicKeyRequest struct {
	BindingID *string `json:"bindingId,omitempty"`
	PatientID *string `json:"patientId,omitempty"`
	PublicKey string  `json:"publicKey"`
}

// PublishFamilyPublicKeyResponse resposta de sucesso na publicação da chave
type PublishFamilyPublicKeyResponse struct {
	Status    string `json:"status"`
	PatientID string `json:"patientId"`
	PublicKey string `json:"publicKey"`
}

// AcceptInvitationRequest payload para aceite de convite por código
type AcceptInvitationRequest struct {
	Code               string  `json:"code"`
	CaregiverName      string  `json:"caregiverName"`
	CaregiverRelation  string  `json:"caregiverRelation"`
	CaregiverPublicKey *string `json:"caregiverPublicKey,omitempty"`
}

// FamilyBinding representa o vínculo familiar entre paciente e cuidador (tabela family_bindings)
type FamilyBinding struct {
	ID                 string    `json:"id" db:"id"`
	PatientID          string    `json:"patient_id" db:"patient_id"`
	PatientName        string    `json:"patient_name,omitempty" db:"patient_name"`
	CaregiverID        *string   `json:"caregiver_id,omitempty" db:"caregiver_id"`
	CaregiverName      string    `json:"caregiver_name" db:"caregiver_name"`
	CaregiverRelation  string    `json:"caregiver_relation" db:"caregiver_relation"`
	ConnectionCode     string    `json:"connection_code" db:"connection_code"`
	Status             string    `json:"status" db:"status"`
	CaregiverRole      string    `json:"caregiver_role" db:"caregiver_role"`
	Permissions        []string  `json:"permissions" db:"permissions"`
	PatientPublicKey   *string   `json:"patient_public_key,omitempty" db:"patient_public_key"`
	CaregiverPublicKey *string   `json:"caregiver_public_key,omitempty" db:"caregiver_public_key"`
	CreatedAt          time.Time `json:"created_at" db:"created_at"`
	ExpiresAt          time.Time `json:"expires_at" db:"expires_at"`
}

// SyncFamilyBindingRequest é o payload local->nuvem do vínculo familiar (Android).
// Fase #7: porta o contrato que ainda vivia em Python (POST /api/sync/family-binding).
type SyncFamilyBindingRequest struct {
	ID                 string   `json:"id"`
	PatientID          string   `json:"patientId"`
	CaregiverID        *string  `json:"caregiverId,omitempty"`
	CaregiverName      string   `json:"caregiverName"`
	CaregiverRelation  string   `json:"caregiverRelation"`
	ConnectionCode     string   `json:"connectionCode"`
	Status             string   `json:"status"`
	CaregiverRole      string   `json:"caregiverRole"`
	Permissions        []string `json:"permissions"`
	PatientPublicKey   *string  `json:"patientPublicKey,omitempty"`
	CaregiverPublicKey *string  `json:"caregiverPublicKey,omitempty"`
	CreatedAt          string   `json:"createdAt"`
	ExpiresAt          string   `json:"expiresAt"`
}

// SyncFamilyBindingResponse é a resposta HTTP 200 para criação/revogação de convite
type SyncFamilyBindingResponse struct {
	Status string `json:"status"`
	ID     string `json:"id"`
}

var allowedBindingRoles = map[string]struct{}{
	"ADMIN_CHILD":        {},
	"CAREGIVER_VIEWER":   {},
	"PROFESSIONAL_NURSE": {},
}

var allowedBindingPermissions = map[string]struct{}{
	"care:read":        {},
	"medication:write": {},
	"medication:take":  {},
	"medical:share":    {},
	"emergency:share":  {},
}

// Padrões de higiene equivalentes ao sanitize_zero_trust_text do Python legado.
var (
	controlCharsPattern = regexp.MustCompile(`[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]`)
	htmlTagPattern      = regexp.MustCompile(`(?i)<[^>]*>`)
)

// NormalizeConnectionCode valida e normaliza o código de convite (8 caracteres A-Z0-9).
func NormalizeConnectionCode(raw string) (string, error) {
	code := strings.ToUpper(strings.TrimSpace(raw))
	if len(code) != 8 {
		return "", ErrInvalidConnectionCode
	}
	for _, char := range code {
		if !((char >= 'A' && char <= 'Z') || (char >= '0' && char <= '9')) {
			return "", ErrInvalidConnectionCode
		}
	}
	return code, nil
}

// ValidateCaregiverRoleAndPermissions aplica as listas fechadas herdadas do contrato Python.
func ValidateCaregiverRoleAndPermissions(role string, permissions []string) error {
	if _, ok := allowedBindingRoles[role]; !ok {
		return ErrInvalidCaregiverRole
	}
	for _, permission := range permissions {
		if _, ok := allowedBindingPermissions[permission]; !ok {
			return ErrInvalidPermissions
		}
	}
	return nil
}

// SanitizeBindingText aplica a mesma higiene do Python (controle/HTML removidos + truncagem).
func SanitizeBindingText(text string, maxLength int) string {
	cleaned := strings.TrimSpace(text)
	cleaned = controlCharsPattern.ReplaceAllString(cleaned, "")
	cleaned = htmlTagPattern.ReplaceAllString(cleaned, "")
	if maxLength <= 0 {
		return cleaned
	}
	runes := []rune(cleaned)
	if len(runes) > maxLength {
		runes = runes[:maxLength]
	}
	return string(runes)
}

// ValidateE2EEPublicKey valida a chave pública ECDH (SubjectPublicKeyInfo Base64) sem chaves privadas no servidor
func ValidateE2EEPublicKey(rawKey string) (string, error) {
	cleaned := strings.TrimSpace(rawKey)
	if cleaned == "" {
		return "", nil
	}
	decoded, err := base64.StdEncoding.DecodeString(cleaned)
	if err != nil {
		return "", ErrInvalidE2EEKey
	}
	if len(decoded) < 32 || len(decoded) > 512 {
		return "", ErrInvalidE2EEKeySize
	}
	return base64.StdEncoding.EncodeToString(decoded), nil
}

// ParseE2EEEnvelope valida o envelope E2EE AES-256-GCM sem decifrar o conteúdo (Blind Relay)
func ParseE2EEEnvelope(rawBlob string, strict bool) (string, error) {
	cleaned := strings.TrimSpace(rawBlob)
	if cleaned == "" {
		if strict {
			return "", ErrEmptyE2EEPayload
		}
		return "", nil
	}
	if !strings.HasPrefix(cleaned, "{") || !strings.HasSuffix(cleaned, "}") {
		if strict {
			return "", ErrMalformedE2EEPayload
		}
		return "", nil
	}

	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(cleaned), &parsed); err != nil {
		if strict {
			return "", ErrInvalidE2EEJSON
		}
		return "", nil
	}

	// Preserva ordem canônica SPEC-13 (v, iv, ciphertext) idêntica ao Python
	vVal, hasV := parsed["v"]
	ivVal, hasIV := parsed["iv"]
	ctVal, hasCT := parsed["ciphertext"]
	if hasV && hasIV && hasCT {
		var vInt int
		switch v := vVal.(type) {
		case float64:
			vInt = int(v)
		case int:
			vInt = v
		}
		ivStr, _ := ivVal.(string)
		ctStr, _ := ctVal.(string)
		canonicalJSON, err := json.Marshal(struct {
			V          int    `json:"v"`
			IV         string `json:"iv"`
			Ciphertext string `json:"ciphertext"`
		}{
			V:          vInt,
			IV:         ivStr,
			Ciphertext: ctStr,
		})
		if err == nil {
			return string(canonicalJSON), nil
		}
	}

	// Normaliza o JSON compactado para armazenamento uniforme
	compactJSON, err := json.Marshal(parsed)
	if err != nil {
		return "", ErrInvalidE2EEJSON
	}
	return string(compactJSON), nil
}

// ParseMessageLifetime calcula e valida sentAt e expiresAt garantindo regra D47 de 24h
func ParseMessageLifetime(sentAtRaw string, now time.Time) (time.Time, time.Time, error) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	cleaned := strings.TrimSpace(sentAtRaw)
	if cleaned == "" {
		return time.Time{}, time.Time{}, ErrInvalidSentAt
	}

	// Suporta ISO 8601 com 'Z' ou offset
	cleaned = strings.ReplaceAll(cleaned, "Z", "+00:00")
	sent, err := time.Parse(time.RFC3339Nano, cleaned)
	if err != nil {
		sent, err = time.Parse(time.RFC3339, cleaned)
		if err != nil {
			return time.Time{}, time.Time{}, ErrInvalidSentAt
		}
	}
	sent = sent.UTC()

	if sent.After(now.Add(5 * time.Minute)) {
		return time.Time{}, time.Time{}, ErrFutureSentAt
	}

	if sent.After(now) {
		sent = now
	}

	expires := sent.Add(24 * time.Hour)
	if !expires.After(now) {
		return time.Time{}, time.Time{}, ErrExpiredMessage
	}

	return sent, expires, nil
}

// MarshalJSON implementa serialização com dual compatibility (camelCase + snake_case)
func (m FamilyMessage) MarshalJSON() ([]byte, error) {
	var delAtISO *string
	if m.DeletedAt != nil {
		s := m.DeletedAt.UTC().Format("2006-01-02T15:04:05.000Z")
		delAtISO = &s
	}
	sentAtISO := m.SentAt.UTC().Format("2006-01-02T15:04:05.000Z")
	expiresAtISO := m.ExpiresAt.UTC().Format("2006-01-02T15:04:05.000Z")

	return json.Marshal(&struct {
		ID                     string  `json:"id"`
		PatientID              string  `json:"patientId"`
		PatientIDSnake         string  `json:"patient_id"`
		SenderID               string  `json:"senderId"`
		SenderIDSnake          string  `json:"sender_id"`
		SenderName             string  `json:"senderName"`
		SenderNameSnake        string  `json:"sender_name"`
		MessageText            string  `json:"messageText"`
		MessageTextSnake       string  `json:"message_text"`
		BodyEncrypted          *string `json:"bodyEncrypted"`
		BodyEncryptedSnake     *string `json:"body_encrypted"`
		EncryptionVersion      int16   `json:"encryptionVersion"`
		EncryptionVersionSnake int16   `json:"encryption_version"`
		IconType               string  `json:"iconType"`
		IconTypeSnake          string  `json:"icon_type"`
		IsRead                 bool    `json:"isRead"`
		IsReadSnake            bool    `json:"is_read"`
		SentAt                 string  `json:"sentAt"`
		SentAtSnake            string  `json:"sent_at"`
		ExpiresAt              string  `json:"expiresAt"`
		ExpiresAtSnake         string  `json:"expires_at"`
		DeletedAt              *string `json:"deletedAt"`
		DeletedAtSnake         *string `json:"deleted_at"`
	}{
		ID:                     m.ID,
		PatientID:              m.PatientID,
		PatientIDSnake:         m.PatientID,
		SenderID:               m.SenderID,
		SenderIDSnake:          m.SenderID,
		SenderName:             m.SenderName,
		SenderNameSnake:        m.SenderName,
		MessageText:            m.MessageText,
		MessageTextSnake:       m.MessageText,
		BodyEncrypted:          m.BodyEncrypted,
		BodyEncryptedSnake:     m.BodyEncrypted,
		EncryptionVersion:      m.EncryptionVersion,
		EncryptionVersionSnake: m.EncryptionVersion,
		IconType:               m.IconType,
		IconTypeSnake:          m.IconType,
		IsRead:                 m.IsRead,
		IsReadSnake:            m.IsRead,
		SentAt:                 sentAtISO,
		SentAtSnake:            sentAtISO,
		ExpiresAt:              expiresAtISO,
		ExpiresAtSnake:         expiresAtISO,
		DeletedAt:              delAtISO,
		DeletedAtSnake:         delAtISO,
	})
}
