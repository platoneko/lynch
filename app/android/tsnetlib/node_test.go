package tsnetlib

import "testing"

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
