package domain

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestValidateE2EEPublicKey(t *testing.T) {
	// 65 bytes raw (chave pública uncompressed secp256r1)
	validRaw := make([]byte, 65)
	validRaw[0] = 0x04
	validBase64 := base64.StdEncoding.EncodeToString(validRaw)

	t.Run("Valid Key", func(t *testing.T) {
		res, err := ValidateE2EEPublicKey(validBase64)
		if err != nil {
			t.Fatalf("Esperava sucesso, obteve erro: %v", err)
		}
		if res != validBase64 {
			t.Errorf("Esperava %s, obteve %s", validBase64, res)
		}
	})

	t.Run("Empty Key", func(t *testing.T) {
		res, err := ValidateE2EEPublicKey("   ")
		if err != nil {
			t.Fatalf("Esperava sucesso em chave vazia, obteve erro: %v", err)
		}
		if res != "" {
			t.Errorf("Esperava string vazia, obteve %s", res)
		}
	})

	t.Run("Invalid Base64", func(t *testing.T) {
		_, err := ValidateE2EEPublicKey("not-a-valid-base64!@#$")
		if err != ErrInvalidE2EEKey {
			t.Errorf("Esperava ErrInvalidE2EEKey, obteve: %v", err)
		}
	})

	t.Run("Too Short Key", func(t *testing.T) {
		shortRaw := make([]byte, 16)
		shortBase64 := base64.StdEncoding.EncodeToString(shortRaw)
		_, err := ValidateE2EEPublicKey(shortBase64)
		if err != ErrInvalidE2EEKeySize {
			t.Errorf("Esperava ErrInvalidE2EEKeySize, obteve: %v", err)
		}
	})
}

func TestParseE2EEEnvelope(t *testing.T) {
	validEnvelope := `{"v":1,"iv":"dGVzdGVpdmJhc2U2NA==","ciphertext":"Y2lwaGVydGV4dGRhdGE="}`

	t.Run("Valid Envelope Strict", func(t *testing.T) {
		res, err := ParseE2EEEnvelope(validEnvelope, true)
		if err != nil {
			t.Fatalf("Esperava sucesso, obteve: %v", err)
		}
		if res == "" {
			t.Error("Envelope não deveria ser vazio")
		}
	})

	t.Run("Malformed JSON Strict", func(t *testing.T) {
		_, err := ParseE2EEEnvelope(`{"v":1, broken}`, true)
		if err != ErrInvalidE2EEJSON {
			t.Errorf("Esperava ErrInvalidE2EEJSON, obteve: %v", err)
		}
	})

	t.Run("Not JSON Non-Strict", func(t *testing.T) {
		res, err := ParseE2EEEnvelope("Texto plano comum", false)
		if err != nil {
			t.Errorf("Não esperava erro em non-strict, obteve: %v", err)
		}
		if res != "" {
			t.Errorf("Esperava string vazia, obteve: %s", res)
		}
	})

	t.Run("Empty Strict", func(t *testing.T) {
		_, err := ParseE2EEEnvelope("", true)
		if err != ErrEmptyE2EEPayload {
			t.Errorf("Esperava ErrEmptyE2EEPayload, obteve: %v", err)
		}
	})
}

func TestFamilyBindingJSONSerialization(t *testing.T) {
	t.Run("Serializes permissions as JSON array", func(t *testing.T) {
		b := FamilyBinding{
			ID:                "uuid-1234",
			PatientID:         "patient-1",
			CaregiverName:     "Welisa",
			CaregiverRelation: "Esposa",
			ConnectionCode:    "EKJM5K0A",
			Status:            "ACTIVE",
			CaregiverRole:     "CAREGIVER_VIEWER",
			Permissions:       []string{},
		}

		data, err := json.Marshal(b)
		if err != nil {
			t.Fatalf("Falha ao serializar FamilyBinding: %v", err)
		}

		str := string(data)
		if !strings.Contains(str, `"permissions":[]`) {
			t.Errorf("Esperava '\"permissions\":[]' no JSON, obteve: %s", str)
		}
		if !strings.Contains(str, `"id":"uuid-1234"`) {
			t.Errorf("Esperava id no JSON, obteve: %s", str)
		}
	})

	t.Run("Serializes permissions with elements", func(t *testing.T) {
		b := FamilyBinding{
			ID:          "uuid-5678",
			PatientID:   "patient-2",
			Permissions: []string{"ALL"},
		}

		data, err := json.Marshal(b)
		if err != nil {
			t.Fatalf("Falha ao serializar FamilyBinding: %v", err)
		}

		str := string(data)
		if !strings.Contains(str, `"permissions":["ALL"]`) {
			t.Errorf("Esperava '\"permissions\":[\"ALL\"]' no JSON, obteve: %s", str)
		}
	})
}

// ---------------------------------------------------------------------------
// Fase #7: validadores do vínculo familiar local->nuvem.
// ---------------------------------------------------------------------------
func TestNormalizeConnectionCode(t *testing.T) {
	t.Run("normalizes lowercase and spaces", func(t *testing.T) {
		code, err := NormalizeConnectionCode("  abcd1234  ")
		if err != nil || code != "ABCD1234" {
			t.Fatalf("Esperava ABCD1234, obteve %q (err=%v)", code, err)
		}
	})

	for _, invalid := range []string{"", "ABC", "ABCD12345", "ABCD-123", "ABCD 234", "abcde12!"} {
		t.Run("rejects "+invalid, func(t *testing.T) {
			if _, err := NormalizeConnectionCode(invalid); !errors.Is(err, ErrInvalidConnectionCode) {
				t.Fatalf("Esperava ErrInvalidConnectionCode para %q, obteve %v", invalid, err)
			}
		})
	}
}

func TestValidateCaregiverRoleAndPermissions(t *testing.T) {
	valid := [][]string{{}, {"care:read"}, {"care:read", "medication:take", "emergency:share"}}
	for _, perms := range valid {
		if err := ValidateCaregiverRoleAndPermissions("CAREGIVER_VIEWER", perms); err != nil {
			t.Fatalf("Permissões válidas %v rejeitadas: %v", perms, err)
		}
	}
	for _, role := range []string{"ROOT", "ADMIN", "admin_child", ""} {
		if err := ValidateCaregiverRoleAndPermissions(role, nil); !errors.Is(err, ErrInvalidCaregiverRole) {
			t.Fatalf("Esperava ErrInvalidCaregiverRole para %q, obteve %v", role, err)
		}
	}
	if err := ValidateCaregiverRoleAndPermissions("ADMIN_CHILD", []string{"care:read", "admin:root"}); !errors.Is(err, ErrInvalidPermissions) {
		t.Fatalf("Esperava ErrInvalidPermissions, obteve %v", err)
	}
}

func TestSanitizeBindingText(t *testing.T) {
	if got := SanitizeBindingText("  <b>Ana</b> Silva  ", 100); got != "Ana Silva" {
		t.Fatalf("Sanitização inesperada: %q", got)
	}
	if got := SanitizeBindingText("Ana\x00Maria", 100); got != "AnaMaria" {
		t.Fatalf("Caracteres de controle deveriam ser removidos: %q", got)
	}
	if got := SanitizeBindingText("abcdefghij", 4); got != "abcd" {
		t.Fatalf("Truncagem inesperada: %q", got)
	}
}
