package domain

import (
	"encoding/base64"
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
