package tsnetlib

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"time"
)

func realDial(ctx context.Context, network, addr string) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, network, addr)
}

func noDialError(t *testing.T) func(error) {
	return func(err error) { t.Errorf("unexpected dial error: %v", err) }
}

// reportTo hands dial errors to ch without ever blocking the forwarder.
func reportTo(ch chan error) func(error) {
	return func(err error) {
		select {
		case ch <- err:
		default:
		}
	}
}

// startForwarder serves f on a fresh loopback port and returns its address.
func startForwarder(t *testing.T, f *forwarder) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go f.serve(ln)
	t.Cleanup(func() { f.close(); ln.Close() })
	return ln.Addr().String()
}

// echoServer echoes every connection back and returns its address.
func echoServer(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() { io.Copy(c, c); c.Close() }()
		}
	}()
	return ln.Addr().String()
}

func dialForward(t *testing.T, addr string) net.Conn {
	t.Helper()
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

// expectEcho sends a frame through addr, wants it back unchanged and returns the open connection.
func expectEcho(t *testing.T, addr string) net.Conn {
	t.Helper()
	c := dialForward(t, addr)
	msg := []byte("orca e2ee frame")
	if _, err := c.Write(msg); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, len(msg))
	c.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.ReadFull(c, got); err != nil {
		t.Fatalf("echo: %v", err)
	}
	c.SetReadDeadline(time.Time{})
	if !bytes.Equal(got, msg) {
		t.Fatalf("echo %q, want %q", got, msg)
	}
	return c
}

// expectClosed fails unless c reaches EOF (or a reset) within five seconds.
func expectClosed(t *testing.T, c net.Conn) {
	t.Helper()
	c.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, err := c.Read(make([]byte, 1))
	if err == nil {
		t.Fatal("read returned data, want the connection closed")
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		t.Fatal("connection still open after 5s")
	}
}

func TestForwardCopiesBothWays(t *testing.T) {
	expectEcho(t, startForwarder(t, newForwarder(echoServer(t), realDial, noDialError(t))))
}

func TestForwardClosesClientWhenTargetCloses(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		if c, err := ln.Accept(); err == nil {
			c.Close()
		}
	}()
	c := dialForward(t, startForwarder(t, newForwarder(ln.Addr().String(), realDial, noDialError(t))))
	expectClosed(t, c)
}

func TestForwardClosesTargetWhenClientCloses(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	targetRead := make(chan error, 1)
	go func() {
		c, err := ln.Accept()
		if err != nil {
			targetRead <- err
			return
		}
		defer c.Close()
		_, err = c.Read(make([]byte, 1))
		targetRead <- err
	}()
	c := dialForward(t, startForwarder(t, newForwarder(ln.Addr().String(), realDial, noDialError(t))))
	c.Close()
	select {
	case err := <-targetRead:
		if err != io.EOF {
			t.Fatalf("target read: %v, want EOF", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("target still open after the client closed")
	}
}

func TestForwardDialFailureClosesAndReports(t *testing.T) {
	reported := make(chan error, 1)
	fail := func(context.Context, string, string) (net.Conn, error) {
		return nil, errors.New("pivot-home is offline")
	}
	c := dialForward(t, startForwarder(t, newForwarder("pivot-home.example.ts.net:6768", fail, reportTo(reported))))
	expectClosed(t, c)
	select {
	case err := <-reported:
		if !strings.Contains(err.Error(), "offline") {
			t.Fatalf("reported %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("dial failure not reported")
	}
}

// At a cold start the node runs before its MagicDNS names resolve; keep dialing.
func TestForwardRetriesUntilTheNameResolves(t *testing.T) {
	defer func(d time.Duration) { lookupRetry = d }(lookupRetry)
	lookupRetry = time.Millisecond
	var mu sync.Mutex
	misses := 0
	dial := func(ctx context.Context, network, addr string) (net.Conn, error) {
		mu.Lock()
		defer mu.Unlock()
		if misses < 3 {
			misses++
			return nil, &net.DNSError{Err: "no such host", Name: "pivot-home.example.ts.net", IsNotFound: true}
		}
		return realDial(ctx, network, addr)
	}
	expectEcho(t, startForwarder(t, newForwarder(echoServer(t), dial, noDialError(t))))
	// Taking mu orders the forwarder's last read of lookupRetry before the deferred restore.
	mu.Lock()
	got := misses
	mu.Unlock()
	if got != 3 {
		t.Fatalf("dialed after %d misses, want 3", got)
	}
}

func TestForwardStopsRetryingAtTheDialDeadline(t *testing.T) {
	defer func(d, r time.Duration) { dialTimeout, lookupRetry = d, r }(dialTimeout, lookupRetry)
	dialTimeout, lookupRetry = 50*time.Millisecond, time.Millisecond
	never := func(context.Context, string, string) (net.Conn, error) {
		return nil, &net.DNSError{Err: "no such host", Name: "pivot-home.example.ts.net", IsNotFound: true}
	}
	reported := make(chan error, 1)
	c := dialForward(t, startForwarder(t, newForwarder("pivot-home.example.ts.net:6768", never, reportTo(reported))))
	expectClosed(t, c)
	select {
	case err := <-reported:
		if !strings.Contains(err.Error(), "no such host") {
			t.Fatalf("reported %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("dial failure not reported")
	}
}

func TestForwardCloseDropsLiveConnections(t *testing.T) {
	f := newForwarder(echoServer(t), realDial, noDialError(t))
	c := expectEcho(t, startForwarder(t, f))
	f.close()
	expectClosed(t, c)
}

// Stop while a dial waits for the node (e.g. before login) must not hang, and
// the cut-short dial is not a failure worth showing.
func TestForwardCloseCancelsPendingDial(t *testing.T) {
	dialing := make(chan struct{})
	hang := func(ctx context.Context, _, _ string) (net.Conn, error) {
		close(dialing)
		<-ctx.Done()
		return nil, ctx.Err()
	}
	f := newForwarder("pivot-home.example.ts.net:6768", hang, noDialError(t))
	c := dialForward(t, startForwarder(t, f))
	select {
	case <-dialing:
	case <-time.After(5 * time.Second):
		t.Fatal("dial never started")
	}
	f.close()
	expectClosed(t, c)
}
