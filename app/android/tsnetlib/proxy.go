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

// tsnet's Dial blocks until the node is Running and the WebView's request has
// no deadline, so an unbounded dial would hang during login or a lost tailnet
// instead of returning the 502 page. A var only so tests can shorten it.
var dialTimeout = 15 * time.Second

// newProxy forwards every request to the single configured target over dial
// (the tailnet in production); nothing in a request can pick another host.
func newProxy(target string, dial dialFunc) http.Handler {
	timeout := dialTimeout
	p := httputil.NewSingleHostReverseProxy(&url.URL{Scheme: "http", Host: target})
	p.Transport = &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			ctx, cancel := context.WithTimeout(ctx, timeout)
			defer cancel()
			return dial(ctx, network, addr)
		},
		ResponseHeaderTimeout: 30 * time.Second,
	}
	p.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		http.Error(w, "tailnet 已连上，但服务端无响应："+err.Error(), http.StatusBadGateway)
	}
	return p
}
