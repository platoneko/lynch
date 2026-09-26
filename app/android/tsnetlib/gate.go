package tsnetlib

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"net/http"
)

// Other apps on the phone can reach 127.0.0.1 too, so the loopback port is not
// a boundary. Only the WebView handed the boot URL learns the per-launch secret.
const (
	bootPath   = "/__boot"
	cookieName = "lynch_k"
)

func newGate(secret string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == bootPath {
			if !sameSecret(r.URL.Query().Get("k"), secret) {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
			http.SetCookie(w, &http.Cookie{
				Name: cookieName, Value: secret, Path: "/",
				HttpOnly: true, SameSite: http.SameSiteStrictMode,
			})
			http.Redirect(w, r, "/", http.StatusFound)
			return
		}
		c, err := r.Cookie(cookieName)
		if err != nil || !sameSecret(c.Value, secret) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func sameSecret(got, want string) bool {
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

func newSecret() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}
