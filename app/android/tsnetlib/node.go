package tsnetlib

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"tailscale.com/logtail"
	"tailscale.com/tsnet"
)

const hostname = "lynch-phone"

var (
	mu  sync.Mutex
	cur *session
)

type session struct {
	srv    *tsnet.Server
	ln     net.Listener
	secret string
	done   chan struct{}

	mu      sync.Mutex
	state   string
	authURL string
	errMsg  string
}

type status struct {
	State   string `json:"state"`
	AuthURL string `json:"auth_url,omitempty"`
	Port    int    `json:"port,omitempty"`
	Error   string `json:"error,omitempty"`
}

// Start brings up the app-private tsnet node and the loopback proxy to target.
// It returns at once; progress is read through Status.
func Start(dataDir, target string) error {
	mu.Lock()
	defer mu.Unlock()
	if cur != nil {
		return errors.New("already started; call Stop first")
	}
	if target == "" {
		return errors.New("server address is empty")
	}

	// Step 1: keep tsnet's state and logs inside the app-private directory.
	logsDir := filepath.Join(dataDir, "logs")
	if err := os.MkdirAll(logsDir, 0o700); err != nil {
		return err
	}
	// On Android tsnet resolves its log directory from TS_LOGS_DIR, not from Dir.
	if err := os.Setenv("TS_LOGS_DIR", logsDir); err != nil {
		return err
	}
	logtail.Disable() // never upload logs from the phone node
	srv := &tsnet.Server{
		Dir:      filepath.Join(dataDir, "tsnet"),
		Hostname: hostname,
		Logf:     func(string, ...any) {},
		UserLogf: func(string, ...any) {},
	}

	// Step 2: the gated proxy listens right away; dials simply fail until the node runs.
	secret, err := newSecret()
	if err != nil {
		return err
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	s := &session{srv: srv, ln: ln, secret: secret, done: make(chan struct{}), state: "starting"}
	go http.Serve(ln, newGate(secret, newProxy(target, srv.Dial)))

	// Step 3: bring the node up and keep Status current in the background.
	go s.run()
	cur = s
	return nil
}

func (s *session) run() {
	if err := s.srv.Start(); err != nil {
		s.set("error", "", err.Error())
		return
	}
	lc, err := s.srv.LocalClient()
	if err != nil {
		s.set("error", "", err.Error())
		return
	}
	loginRequested := false
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-s.done:
			return
		case <-tick.C:
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		st, err := lc.Status(ctx)
		// A fresh node only gets a login URL after an interactive login is requested.
		if err == nil && st.BackendState == "NeedsLogin" && st.AuthURL == "" && !loginRequested {
			loginRequested = true
			err = lc.StartLoginInteractive(ctx)
		}
		cancel()
		if err != nil {
			s.set("error", "", err.Error())
			continue
		}
		s.set(stateOf(st.BackendState), st.AuthURL, "")
	}
}

func stateOf(backend string) string {
	switch backend {
	case "Running":
		return "running"
	case "NeedsLogin":
		return "needs_login"
	case "NeedsMachineAuth":
		return "needs_approval"
	default:
		return "starting"
	}
}

func (s *session) set(state, authURL, errMsg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state, s.authURL, s.errMsg = state, authURL, errMsg
}

func (s *session) isRunning() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state == "running"
}

func (s *session) port() int { return s.ln.Addr().(*net.TCPAddr).Port }

func currentSession() *session {
	mu.Lock()
	defer mu.Unlock()
	return cur
}

// Status reports progress as JSON for the Kotlin shell.
func Status() string {
	s := currentSession()
	if s == nil {
		return `{"state":"stopped"}`
	}
	s.mu.Lock()
	st := status{State: s.state, AuthURL: s.authURL, Port: s.port(), Error: s.errMsg}
	s.mu.Unlock()
	b, _ := json.Marshal(st)
	return string(b)
}

// BootURL is the one URL the WebView loads first; it carries the per-launch secret.
func BootURL() string {
	s := currentSession()
	if s == nil {
		return ""
	}
	return fmt.Sprintf("http://127.0.0.1:%d%s?k=%s", s.port(), bootPath, s.secret)
}

// Stop closes the proxy and the node; the node identity stays on disk.
func Stop() {
	mu.Lock()
	s := cur
	cur = nil
	mu.Unlock()
	if s == nil {
		return
	}
	close(s.done)
	s.ln.Close()
	s.srv.Close()
}
