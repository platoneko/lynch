package tsnetlib

import (
	"context"
	"errors"
	"fmt"
	"html"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"
)

type dialFunc func(ctx context.Context, network, addr string) (net.Conn, error)

// tsnet's Dial blocks until the node is Running and the WebView's request has
// no deadline, so an unbounded dial would hang during login or a lost tailnet
// instead of returning the 502 page. A var only so tests can shorten it.
var dialTimeout = 15 * time.Second

// Right after the node reaches Running its netmap may not be in yet, so the
// MagicDNS target falls through to system DNS and "no such host" (every cold
// start). Redial at this interval until the name resolves or the dial times out.
var lookupRetry = 300 * time.Millisecond

// newProxy forwards every request to the single configured target over dial
// (the tailnet in production); nothing in a request can pick another host.
func newProxy(target string, dial dialFunc) http.Handler {
	timeout := dialTimeout
	p := httputil.NewSingleHostReverseProxy(&url.URL{Scheme: "http", Host: target})
	p.Transport = &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			ctx, cancel := context.WithTimeout(ctx, timeout)
			defer cancel()
			return dialUntilResolved(ctx, dial, network, addr)
		},
		ResponseHeaderTimeout: 30 * time.Second,
	}
	p.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		msg := "tailnet 已连上，但服务端无响应：" + err.Error()
		if !isNavigation(r) {
			// fetch() callers (web api.ts) show this body verbatim.
			http.Error(w, msg, http.StatusBadGateway)
			return
		}
		// A page load that fails never starts the web app and its retry button,
		// so the error page must carry its own.
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprintf(w, retryPage, html.EscapeString(msg))
	}
	return p
}

func dialUntilResolved(ctx context.Context, dial dialFunc, network, addr string) (net.Conn, error) {
	for {
		conn, err := dial(ctx, network, addr)
		var dnsErr *net.DNSError
		if err == nil || !errors.As(err, &dnsErr) || !dnsErr.IsNotFound {
			return conn, err
		}
		select {
		case <-ctx.Done():
			return nil, err // the lookup error says more than "deadline exceeded"
		case <-time.After(lookupRetry):
		}
	}
}

func isNavigation(r *http.Request) bool {
	return r.Header.Get("Sec-Fetch-Mode") == "navigate" ||
		strings.Contains(r.Header.Get("Accept"), "text/html")
}

const retryPage = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>lynch</title></head>
<body style="font-family:sans-serif;padding:16px">
<p>%s</p>
<button onclick="location.reload()">重试</button>
</body></html>
`
