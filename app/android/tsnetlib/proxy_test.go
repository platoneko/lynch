package tsnetlib

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestProxyOnlyReachesTarget(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "path=%s", r.URL.Path)
	}))
	defer backend.Close()
	target := strings.TrimPrefix(backend.URL, "http://")

	var mu sync.Mutex
	var dialed []string
	dial := func(ctx context.Context, network, addr string) (net.Conn, error) {
		mu.Lock()
		dialed = append(dialed, addr)
		mu.Unlock()
		return (&net.Dialer{}).DialContext(ctx, network, addr)
	}
	front := httptest.NewServer(newProxy(target, dial))
	defer front.Close()

	req, _ := http.NewRequest(http.MethodGet, front.URL+"/api/tickers", nil)
	req.Host = "evil.example:80" // the request must not steer the destination
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "path=/api/tickers" {
		t.Fatalf("body %q", body)
	}
	mu.Lock()
	defer mu.Unlock()
	for _, a := range dialed {
		if a != target {
			t.Fatalf("dialed %q, want only %q", a, target)
		}
	}
}

func TestProxyReportsDeadServer(t *testing.T) {
	front := httptest.NewServer(newProxy("127.0.0.1:1", (&net.Dialer{}).DialContext))
	defer front.Close()
	resp, err := http.Get(front.URL + "/api/tickers")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway || !strings.Contains(string(body), "服务端无响应") {
		t.Fatalf("got %d %q", resp.StatusCode, body)
	}
}
