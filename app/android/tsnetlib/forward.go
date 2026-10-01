package tsnetlib

import (
	"context"
	"errors"
	"io"
	"net"
	"sync"
	"time"
)

type dialFunc func(ctx context.Context, network, addr string) (net.Conn, error)

// tsnet's Dial blocks until the node is Running, so an unbounded dial would hold a
// client's connection open for the whole login. A var only so tests can shorten it.
var dialTimeout = 15 * time.Second

// Right after the node reaches Running its netmap may not be in yet, so the
// MagicDNS target falls through to system DNS and "no such host" (every cold
// start). Redial at this interval until the name resolves or the dial times out.
var lookupRetry = 300 * time.Millisecond

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

// forwarder pipes each loopback connection to the one fixed target over dial
// (the tailnet in production), byte for byte; it never reads the stream, so the
// client must bring its own auth (Orca Mobile does: E2EE + device token).
type forwarder struct {
	target      string
	dial        dialFunc
	onDialError func(error)

	// ctx ends on close, so a dial still waiting for the node gives up when the forward closes.
	ctx    context.Context
	cancel context.CancelFunc

	mu     sync.Mutex
	conns  map[net.Conn]struct{}
	closed bool
}

func newForwarder(target string, dial dialFunc, onDialError func(error)) *forwarder {
	ctx, cancel := context.WithCancel(context.Background())
	return &forwarder{
		target: target, dial: dial, onDialError: onDialError,
		ctx: ctx, cancel: cancel, conns: map[net.Conn]struct{}{},
	}
}

func (f *forwarder) serve(ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			return // the forward was closed
		}
		go f.handle(c)
	}
}

func (f *forwarder) handle(client net.Conn) {
	if !f.track(client) {
		return
	}
	defer f.untrack(client)

	ctx, cancel := context.WithTimeout(f.ctx, dialTimeout)
	upstream, err := dialUntilResolved(ctx, f.dial, "tcp", f.target)
	cancel()
	if err != nil {
		client.Close()
		if f.ctx.Err() == nil { // a dial cut short by close is not a failure
			f.onDialError(err)
		}
		return
	}
	if !f.track(upstream) {
		client.Close()
		return
	}
	defer f.untrack(upstream)
	pipe(client, upstream)
}

// pipe copies both ways and closes both ends once either direction ends: the
// clients here (WebSocket) never half-close, so one EOF means the session is over.
func pipe(a, b net.Conn) {
	done := make(chan struct{}, 2)
	go func() { io.Copy(a, b); done <- struct{}{} }()
	go func() { io.Copy(b, a); done <- struct{}{} }()
	<-done
	a.Close()
	b.Close()
	<-done
}

// track registers c so close can drop it; once closed it closes c and returns false.
func (f *forwarder) track(c net.Conn) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed {
		c.Close()
		return false
	}
	f.conns[c] = struct{}{}
	return true
}

func (f *forwarder) untrack(c net.Conn) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.conns, c)
}

func (f *forwarder) close() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed = true
	f.cancel()
	for c := range f.conns {
		c.Close()
	}
}
