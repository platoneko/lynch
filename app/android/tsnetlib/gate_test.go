package tsnetlib

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func serve(h http.Handler, path string, cookie *http.Cookie) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	if cookie != nil {
		req.AddCookie(cookie)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestGate(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { io.WriteString(w, "ok") })
	h := newGate("s3cret", ok)

	if rec := serve(h, "/api/tickers", nil); rec.Code != http.StatusForbidden {
		t.Fatalf("no cookie: got %d", rec.Code)
	}
	if rec := serve(h, "/", &http.Cookie{Name: cookieName, Value: "wrong"}); rec.Code != http.StatusForbidden {
		t.Fatalf("wrong cookie: got %d", rec.Code)
	}
	if rec := serve(h, bootPath+"?k=wrong", nil); rec.Code != http.StatusForbidden {
		t.Fatalf("wrong boot key: got %d", rec.Code)
	}

	rec := serve(h, bootPath+"?k=s3cret", nil)
	if rec.Code != http.StatusFound || rec.Header().Get("Location") != "/" {
		t.Fatalf("boot: got %d location=%q", rec.Code, rec.Header().Get("Location"))
	}
	set := rec.Header().Get("Set-Cookie")
	for _, want := range []string{cookieName + "=s3cret", "HttpOnly", "SameSite=Strict", "Path=/"} {
		if !strings.Contains(set, want) {
			t.Fatalf("Set-Cookie %q lacks %q", set, want)
		}
	}

	rec = serve(h, "/api/tickers", &http.Cookie{Name: cookieName, Value: "s3cret"})
	if rec.Code != http.StatusOK || rec.Body.String() != "ok" {
		t.Fatalf("with cookie: got %d %q", rec.Code, rec.Body.String())
	}
}
