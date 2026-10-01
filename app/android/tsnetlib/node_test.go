package tsnetlib

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/cookiejar"
	"strconv"
	"strings"
	"testing"
)

// freePort finds a loopback port nothing is listening on right now.
func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func TestStoppedState(t *testing.T) {
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() = %s", got)
	}
	if BootURL() != "" {
		t.Fatal("BootURL before Start must be empty")
	}
	if err := Start(t.TempDir(), "lynch-phone", "", freePort(t)); err == nil {
		t.Fatal("empty target must be rejected")
	}
}

// A rotation or quick back-out can Stop while the node is still inside srv.Start.
func TestStopRightAfterStart(t *testing.T) {
	if err := Start(t.TempDir(), "lynch-phone", "127.0.0.1:1", freePort(t)); err != nil {
		t.Fatal(err)
	}
	Stop()
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() after Stop = %s", got)
	}
}

func TestStateOf(t *testing.T) {
	for backend, want := range map[string]string{
		"Running": "running", "NeedsLogin": "needs_login", "NeedsMachineAuth": "needs_approval",
		"Starting": "starting", "NoState": "starting",
	} {
		if got := stateOf(backend); got != want {
			t.Fatalf("stateOf(%s) = %s, want %s", backend, got, want)
		}
	}
}

func TestEmptyHostnameRejected(t *testing.T) {
	if err := Start(t.TempDir(), "", "127.0.0.1:1", freePort(t)); err == nil {
		Stop()
		t.Fatal("empty hostname must be rejected")
	}
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() after rejected hostname = %s", got)
	}
}

func TestPortOutOfRangeRejected(t *testing.T) {
	for _, port := range []int{0, 80, 70000} {
		if err := Start(t.TempDir(), "lynch-phone", "127.0.0.1:1", port); err == nil {
			Stop()
			t.Fatalf("port %d must be rejected", port)
		}
		if got := Status(); got != `{"state":"stopped"}` {
			t.Fatalf("Status() after rejected port %d = %s", port, got)
		}
	}
}

func TestBusyPortFails(t *testing.T) {
	busy, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer busy.Close()
	port := busy.Addr().(*net.TCPAddr).Port

	err = Start(t.TempDir(), "lynch-phone", "127.0.0.1:1", port)
	if err == nil {
		Stop()
		t.Fatalf("Start on busy port %d must fail", port)
	}
	if !strings.Contains(err.Error(), strconv.Itoa(port)) {
		t.Fatalf("error does not name the port %d: %v", port, err)
	}
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() after failed Start = %s", got)
	}
}

// The origin is stable across restarts, so a pooled keep-alive connection
// outlives Stop; the new session's boot must not be served on it by the old gate.
func TestRestartOnSamePortAcceptsNewBoot(t *testing.T) {
	dir, port := t.TempDir(), freePort(t)
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{
		Jar:           jar,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}

	for _, round := range []string{"first", "second"} {
		if err := Start(dir, "lynch-phone", "127.0.0.1:1", port); err != nil {
			t.Fatalf("%s Start: %v", round, err)
		}
		boot := BootURL()
		if !strings.HasPrefix(boot, fmt.Sprintf("http://127.0.0.1:%d/", port)) {
			Stop()
			t.Fatalf("%s BootURL %q not on port %d", round, boot, port)
		}
		resp, err := client.Get(boot)
		if err != nil {
			Stop()
			t.Fatalf("%s boot: %v", round, err)
		}
		// Drain before Close so the connection goes back to the client's pool.
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		Stop()
		if resp.StatusCode != http.StatusFound {
			t.Fatalf("%s boot: got %d, want 302", round, resp.StatusCode)
		}
	}
}
