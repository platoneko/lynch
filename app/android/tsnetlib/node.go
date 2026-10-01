package tsnetlib

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"syscall"
	"time"

	"tailscale.com/logtail"
	"tailscale.com/tsnet"
)

var (
	// mu guards cur and the current session's forward table.
	mu  sync.Mutex
	cur *session
)

// session is one running node and the named forwards that share it (and its login).
type session struct {
	srv  *tsnet.Server
	done chan struct{}
	// started closes once srv.Start has returned: tsnet.Server.Close must not
	// run before or concurrently with Start.
	started  chan struct{}
	forwards map[string]*forward // by id; guarded by the package mu

	mu      sync.Mutex // guards the node state below
	state   string
	authURL string
	errMsg  string
}

// forward is one named loopback port and the forwarder behind it.
type forward struct {
	ln net.Listener
	f  *forwarder

	mu        sync.Mutex
	dialErr   string
	dialErrAt int64 // Unix ms of dialErr
}

type status struct {
	State    string          `json:"state"`
	AuthURL  string          `json:"auth_url,omitempty"`
	Error    string          `json:"error,omitempty"`
	Forwards []forwardStatus `json:"forwards,omitempty"`
}

type forwardStatus struct {
	ID          string `json:"id"`
	Port        int    `json:"port"`
	DialError   string `json:"dial_error,omitempty"`
	DialErrorAt int64  `json:"dial_error_at,omitempty"`
}

// StartNode brings up the app-private tsnet node and returns at once; progress is
// read through Status. Forwards are opened on it with OpenForward.
func StartNode(dataDir, hostname string) error {
	mu.Lock()
	defer mu.Unlock()
	if cur != nil {
		return errors.New("already started; call Stop first")
	}
	if hostname == "" {
		return errors.New("hostname is empty")
	}

	// Keep tsnet's state and logs inside the app-private directory.
	logsDir := filepath.Join(dataDir, "logs")
	if err := os.MkdirAll(logsDir, 0o700); err != nil {
		return err
	}
	// On Android tsnet resolves its log directory from TS_LOGS_DIR, not from Dir.
	if err := os.Setenv("TS_LOGS_DIR", logsDir); err != nil {
		return err
	}
	logtail.Disable() // never upload logs from the phone node
	s := &session{
		srv: &tsnet.Server{
			Dir:      filepath.Join(dataDir, "tsnet"),
			Hostname: hostname,
			Logf:     func(string, ...any) {},
			UserLogf: func(string, ...any) {},
		},
		done:     make(chan struct{}),
		started:  make(chan struct{}),
		forwards: map[string]*forward{},
		state:    "starting",
	}
	go s.run()
	cur = s
	return nil
}

func (s *session) run() {
	err := s.srv.Start()
	close(s.started)
	if err != nil {
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

func currentSession() *session {
	mu.Lock()
	defer mu.Unlock()
	return cur
}

// OpenForward listens on 127.0.0.1:port at once and pipes each connection to
// target over the node; until the node is up a connection waits for its dial
// (see dialTimeout). Anything on the phone can reach the port, so the client
// must bring its own auth. A refusal leaves every other forward as it was.
func OpenForward(id string, port int, target string) error {
	if id == "" {
		return errors.New("forward id is empty")
	}
	if port < 1024 || port > 65535 {
		return fmt.Errorf("本机端口 %d 超出 1024–65535", port)
	}
	if err := checkTarget(target); err != nil {
		return err
	}
	mu.Lock()
	defer mu.Unlock()
	if cur == nil {
		return errors.New("节点未启动")
	}
	if _, ok := cur.forwards[id]; ok {
		return fmt.Errorf("转发 %s 已经打开", id)
	}
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		if errors.Is(err, syscall.EADDRINUSE) {
			return fmt.Errorf("端口 %d 被占用", port)
		}
		return fmt.Errorf("端口 %d 无法使用：%w", port, err)
	}
	fw := &forward{ln: ln}
	fw.f = newForwarder(target, cur.srv.Dial, fw.setDialError)
	go fw.f.serve(ln)
	cur.forwards[id] = fw
	return nil
}

// checkTarget refuses a target that would open fine and then fail every dial.
func checkTarget(target string) error {
	host, port, err := net.SplitHostPort(target)
	if err == nil && host != "" {
		if n, err := strconv.Atoi(port); err == nil && n >= 1 && n <= 65535 {
			return nil
		}
	}
	return fmt.Errorf("目标地址 %q 要写成 主机:端口（端口 1–65535）", target)
}

// CloseForward closes one forward: its port is free when this returns, its
// connections are dropped and its pending dials cancelled. An unknown id is a no-op.
func CloseForward(id string) {
	mu.Lock()
	defer mu.Unlock()
	if cur == nil {
		return
	}
	if fw, ok := cur.forwards[id]; ok {
		delete(cur.forwards, id)
		fw.close()
	}
}

func (fw *forward) close() {
	fw.f.close()
	fw.ln.Close()
}

func (fw *forward) setDialError(err error) {
	fw.mu.Lock()
	defer fw.mu.Unlock()
	fw.dialErr, fw.dialErrAt = err.Error(), time.Now().UnixMilli()
}

func (fw *forward) status(id string) forwardStatus {
	fw.mu.Lock()
	defer fw.mu.Unlock()
	return forwardStatus{
		ID: id, Port: fw.ln.Addr().(*net.TCPAddr).Port,
		DialError: fw.dialErr, DialErrorAt: fw.dialErrAt,
	}
}

// Status reports the node and its open forwards as JSON for the Kotlin shell.
func Status() string {
	mu.Lock()
	defer mu.Unlock()
	if cur == nil {
		return `{"state":"stopped"}`
	}
	cur.mu.Lock()
	st := status{State: cur.state, AuthURL: cur.authURL, Error: cur.errMsg}
	cur.mu.Unlock()
	for id, fw := range cur.forwards {
		st.Forwards = append(st.Forwards, fw.status(id))
	}
	sort.Slice(st.Forwards, func(i, j int) bool { return st.Forwards[i].ID < st.Forwards[j].ID })
	b, _ := json.Marshal(st)
	return string(b)
}

// Stop closes every forward, then the node; the node identity stays on disk.
func Stop() {
	mu.Lock()
	s := cur
	cur = nil
	if s != nil {
		for _, fw := range s.forwards {
			fw.close()
		}
	}
	mu.Unlock()
	if s == nil {
		return
	}
	close(s.done)
	<-s.started
	// A Start that failed before tsnet built its system (bad Dir) has nothing to
	// close, and tsnet's Close would dereference the missing system.
	if s.srv.Sys() != nil {
		s.srv.Close()
	}
}
