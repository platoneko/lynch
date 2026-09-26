package tsnetlib

import (
	"context"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"
)

type dialFunc func(ctx context.Context, network, addr string) (net.Conn, error)

// newProxy forwards every request to the single configured target over dial
// (the tailnet in production); nothing in a request can pick another host.
func newProxy(target string, dial dialFunc) http.Handler {
	p := httputil.NewSingleHostReverseProxy(&url.URL{Scheme: "http", Host: target})
	p.Transport = &http.Transport{
		DialContext:           dial,
		ResponseHeaderTimeout: 30 * time.Second,
	}
	p.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		http.Error(w, "tailnet 已连上，但服务端无响应："+err.Error(), http.StatusBadGateway)
	}
	return p
}
