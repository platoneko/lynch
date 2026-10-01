package tsnetlib

import (
	"context"
	"io"
	"net"
	"sync"
)

// forwarder pipes each loopback connection to the one fixed target over dial
// (the tailnet in production), byte for byte; it never reads the stream, so the
// client must bring its own auth (Orca Mobile does: E2EE + device token).
type forwarder struct {
	target      string
	dial        dialFunc
	onDialError func(error)

	// ctx ends on close, so a dial still waiting for the node gives up at Stop.
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
			return // Stop closed the listener
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
		if f.ctx.Err() == nil { // a dial cut short by Stop is not a failure
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
