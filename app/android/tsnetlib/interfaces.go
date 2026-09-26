package tsnetlib

import (
	"encoding/json"
	"fmt"
	"net"
	"sync"
	"sync/atomic"

	"tailscale.com/net/netmon"
)

var (
	ifaceSnapshot  atomic.Pointer[[]netmon.Interface]
	registerIfaces sync.Once
)

// SetInterfaces receives the phone's interfaces from Kotlin: since Android 11 an
// app cannot list them via netlink, which is what Go's net package relies on.
// Kotlin calls it before Start and again on every network change.
func SetInterfaces(rowsJSON string) error {
	var rows []struct {
		Name      string   `json:"name"`
		Index     int      `json:"index"`
		MTU       int      `json:"mtu"`
		Flags     uint     `json:"flags"`
		Addresses []string `json:"addresses"`
	}
	if err := json.Unmarshal([]byte(rowsJSON), &rows); err != nil {
		return err
	}
	ifaces := make([]netmon.Interface, 0, len(rows))
	for _, row := range rows {
		addrs := make([]net.Addr, 0, len(row.Addresses))
		for _, a := range row.Addresses {
			ip, prefix, err := net.ParseCIDR(a)
			if err != nil {
				return fmt.Errorf("interface %s: %w", row.Name, err)
			}
			prefix.IP = ip
			addrs = append(addrs, prefix)
		}
		ifaces = append(ifaces, netmon.Interface{
			Interface: &net.Interface{Name: row.Name, Index: row.Index, MTU: row.MTU, Flags: net.Flags(row.Flags)},
			AltAddrs:  addrs,
		})
	}
	ifaceSnapshot.Store(&ifaces)
	registerIfaces.Do(func() {
		netmon.RegisterInterfaceGetter(func() ([]netmon.Interface, error) { return *ifaceSnapshot.Load(), nil })
	})
	// A live node must re-read interfaces now, e.g. after Wi-Fi → mobile data.
	if s := currentSession(); s != nil && s.isRunning() {
		s.srv.Sys().NetMon.Get().Poll()
	}
	return nil
}
