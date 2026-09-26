package tsnetlib

import "testing"

func TestSetInterfacesParsesRows(t *testing.T) {
	err := SetInterfaces(`[{"name":"wlan0","index":3,"mtu":1500,"flags":33,"addresses":["10.0.0.5/24","fe80::1/64"]}]`)
	if err != nil {
		t.Fatal(err)
	}
	got := *ifaceSnapshot.Load()
	if len(got) != 1 || got[0].Name != "wlan0" || len(got[0].AltAddrs) != 2 {
		t.Fatalf("snapshot %+v", got)
	}
}

func TestSetInterfacesRejectsBareIP(t *testing.T) {
	if err := SetInterfaces(`[{"name":"wlan0","index":3,"mtu":1500,"flags":33,"addresses":["10.0.0.5"]}]`); err == nil {
		t.Fatal("want error for address without prefix")
	}
}
