package tsnetlib

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"testing"
)

func TestStoppedState(t *testing.T) {
	if got := Status(); got != `{"state":"stopped"}` {
		t.Fatalf("Status() = %s", got)
	}
	if BootURL() != "" {
		t.Fatal("BootURL before Start must be empty")
	}
	if err := Start(t.TempDir(), ""); err == nil {
		t.Fatal("empty target must be rejected")
	}
}

// A rotation or quick back-out can Stop while the node is still inside srv.Start.
func TestStopRightAfterStart(t *testing.T) {
	if err := Start(t.TempDir(), "127.0.0.1:1"); err != nil {
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

func startedPort(t *testing.T, dataDir string) int {
	t.Helper()
	if err := Start(dataDir, "127.0.0.1:1"); err != nil {
		t.Fatal(err)
	}
	defer Stop()
	var st status
	if err := json.Unmarshal([]byte(Status()), &st); err != nil {
		t.Fatal(err)
	}
	if st.Port == 0 {
		t.Fatalf("no port in Status()")
	}
	return st.Port
}

// localStorage is per-origin and the origin includes the port, so the port
// must survive restarts for saved filters to survive them.
func TestPortStableAcrossRestarts(t *testing.T) {
	dir := t.TempDir()
	first := startedPort(t, dir)
	if second := startedPort(t, dir); second != first {
		t.Fatalf("port changed across restarts: %d then %d", first, second)
	}
}

func TestCorruptPortFileFallsBack(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "proxy-port"), []byte("garbage\x00"), 0o600); err != nil {
		t.Fatal(err)
	}
	port := startedPort(t, dir)
	if again := startedPort(t, dir); again != port {
		t.Fatalf("fresh port not saved: %d then %d", port, again)
	}
}

func TestBusySavedPortFallsBack(t *testing.T) {
	dir := t.TempDir()
	port := startedPort(t, dir)
	busy, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		t.Fatal(err)
	}
	defer busy.Close()
	if got := startedPort(t, dir); got == port {
		t.Fatalf("reused busy port %d", got)
	}
}
