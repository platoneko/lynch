package tsnetlib

import (
	"encoding/json"
	"fmt"
	"net"
	"strconv"
	"strings"
	"testing"
	"time"
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

func portFree(port int) bool {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		return false
	}
	ln.Close()
	return true
}

func accepts(port int) bool {
	c, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		return false
	}
	c.Close()
	return true
}

func startNode(t *testing.T) {
	t.Helper()
	if err := StartNode(t.TempDir(), "tailfwd-phone"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(Stop)
}

func mustOpen(t *testing.T, id string, port int) {
	t.Helper()
	if err := OpenForward(id, port, "127.0.0.1:1"); err != nil {
		t.Fatalf("OpenForward(%s, %d): %v", id, port, err)
	}
}

func parseStatus(t *testing.T) status {
	t.Helper()
	var st status
	if err := json.Unmarshal([]byte(Status()), &st); err != nil {
		t.Fatal(err)
	}
	return st
}

func openIDs(t *testing.T) []string {
	t.Helper()
	var ids []string
	for _, f := range parseStatus(t).Forwards {
		ids = append(ids, f.ID)
	}
	return ids
}

func TestStoppedState(t *testing.T) {
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() = %s", got)
	}
	if err := OpenForward("orca", freePort(t), "127.0.0.1:1"); err == nil {
		t.Fatal("OpenForward without a node must fail")
	}
	CloseForward("orca") // no node: a no-op
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

func TestStartNodeRejectsEmptyHostname(t *testing.T) {
	if err := StartNode(t.TempDir(), ""); err == nil {
		Stop()
		t.Fatal("empty hostname must be rejected")
	}
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() after rejected hostname = %s", got)
	}
}

func TestStartNodeTwiceFails(t *testing.T) {
	startNode(t)
	if err := StartNode(t.TempDir(), "tailfwd-phone"); err == nil {
		t.Fatal("a second StartNode must fail")
	}
}

// A quick back-out can Stop while the node is still inside srv.Start.
func TestStopRightAfterStartNode(t *testing.T) {
	if err := StartNode(t.TempDir(), "tailfwd-phone"); err != nil {
		t.Fatal(err)
	}
	Stop()
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() after Stop = %s", got)
	}
}

func TestOpenForwardRejectsBadArguments(t *testing.T) {
	startNode(t)
	port := freePort(t)
	for _, c := range []struct {
		id     string
		port   int
		target string
	}{
		{"", port, "127.0.0.1:1"},
		{"orca", 0, "127.0.0.1:1"}, {"orca", 80, "127.0.0.1:1"}, {"orca", 70000, "127.0.0.1:1"},
		{"orca", port, ""}, {"orca", port, "pivot-home.tail090ab2.ts.net"}, {"orca", port, "pivot-home:"},
		{"orca", port, ":6768"}, {"orca", port, "pivot-home:http"}, {"orca", port, "pivot-home:0"},
		{"orca", port, "pivot-home:70000"},
	} {
		if err := OpenForward(c.id, c.port, c.target); err == nil {
			t.Fatalf("OpenForward(%q, %d, %q) must be rejected", c.id, c.port, c.target)
		}
	}
	if ids := openIDs(t); len(ids) != 0 {
		t.Fatalf("open forwards after rejections: %v", ids)
	}
	if !portFree(port) {
		t.Fatalf("port %d left listening by a rejected open", port)
	}
}

func TestTwoForwardsCloseOneKeepsTheOther(t *testing.T) {
	startNode(t)
	orca, lynch := freePort(t), freePort(t)
	mustOpen(t, "orca", orca)
	mustOpen(t, "lynch", lynch)
	if !accepts(orca) || !accepts(lynch) {
		t.Fatal("both forwards must accept connections")
	}
	if got := openIDs(t); strings.Join(got, ",") != "lynch,orca" {
		t.Fatalf("open forwards = %v, want [lynch orca] (sorted by id)", got)
	}
	CloseForward("orca")
	if !portFree(orca) {
		t.Fatalf("port %d not free right after CloseForward", orca)
	}
	if !accepts(lynch) {
		t.Fatal("closing orca must not touch lynch")
	}
	if got := openIDs(t); strings.Join(got, ",") != "lynch" {
		t.Fatalf("open forwards = %v, want [lynch]", got)
	}
}

func TestOpenForwardSamePortTwiceFails(t *testing.T) {
	startNode(t)
	port := freePort(t)
	mustOpen(t, "orca", port)
	err := OpenForward("lynch", port, "127.0.0.1:1")
	if err == nil {
		t.Fatal("a second forward on the same port must fail")
	}
	if !strings.Contains(err.Error(), strconv.Itoa(port)) || !strings.Contains(err.Error(), "被占用") {
		t.Fatalf("error %q should say port %d is taken", err, port)
	}
	if !accepts(port) {
		t.Fatal("the first forward must keep serving")
	}
	if got := openIDs(t); strings.Join(got, ",") != "orca" {
		t.Fatalf("open forwards = %v, want [orca]", got)
	}
}

func TestOpenForwardSameIDTwiceFails(t *testing.T) {
	startNode(t)
	first, second := freePort(t), freePort(t)
	mustOpen(t, "orca", first)
	if err := OpenForward("orca", second, "127.0.0.1:1"); err == nil {
		t.Fatal("the same id twice must fail")
	}
	if !portFree(second) {
		t.Fatalf("port %d left listening by a rejected open", second)
	}
}

// Editing a forward is a close then an open of the same id, often on the same port.
func TestReopenSameIDAndPortAfterClose(t *testing.T) {
	startNode(t)
	port := freePort(t)
	mustOpen(t, "orca", port)
	CloseForward("orca")
	mustOpen(t, "orca", port)
	if !accepts(port) {
		t.Fatal("reopened forward must accept")
	}
}

func TestCloseForwardUnknownIDIsANoOp(t *testing.T) {
	startNode(t)
	port := freePort(t)
	mustOpen(t, "orca", port)
	CloseForward("nope")
	if !accepts(port) {
		t.Fatal("closing an unknown id must not touch other forwards")
	}
}

func TestStopClosesEveryForward(t *testing.T) {
	if err := StartNode(t.TempDir(), "tailfwd-phone"); err != nil {
		t.Fatal(err)
	}
	orca, lynch := freePort(t), freePort(t)
	mustOpen(t, "orca", orca)
	mustOpen(t, "lynch", lynch)
	Stop()
	if !portFree(orca) || !portFree(lynch) {
		t.Fatal("Stop must free every forward's port at once")
	}
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() after Stop = %s", got)
	}
	// The next node starts with no forwards.
	startNode(t)
	if ids := openIDs(t); len(ids) != 0 {
		t.Fatalf("a new node starts with forwards %v", ids)
	}
}

// Before login the node cannot dial; the failure must be listed under that forward only.
func TestStatusReportsDialErrorsPerForward(t *testing.T) {
	defer func(d time.Duration) { dialTimeout = d }(dialTimeout)
	dialTimeout = 50 * time.Millisecond
	startNode(t)
	orca, lynch := freePort(t), freePort(t)
	mustOpen(t, "orca", orca)
	mustOpen(t, "lynch", lynch)
	c, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", orca))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	expectClosed(t, c)
	deadline := time.Now().Add(10 * time.Second)
	for !strings.Contains(Status(), `"dial_error":`) {
		if time.Now().After(deadline) {
			t.Fatalf("Status() = %s, want a dial_error", Status())
		}
		time.Sleep(10 * time.Millisecond)
	}
	seen := map[string]bool{}
	for _, f := range parseStatus(t).Forwards {
		seen[f.ID] = true
		switch f.ID {
		case "orca":
			if f.DialError == "" || f.DialErrorAt == 0 || f.Port != orca {
				t.Fatalf("orca status = %+v, want its dial error, time and port %d", f, orca)
			}
		case "lynch":
			if f.DialError != "" || f.DialErrorAt != 0 || f.Port != lynch {
				t.Fatalf("lynch status = %+v, want no dial error and port %d", f, lynch)
			}
		}
	}
	if !seen["orca"] || !seen["lynch"] {
		t.Fatalf("Status() = %s, want both orca and lynch", Status())
	}
}
