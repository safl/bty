package machine

import (
	. "github.com/safl/bty/osi"
	. "github.com/safl/bty/bzi"
	. "github.com/safl/bty/pxe"
)

type Machine struct {
	hwa		string		`json:"hwa"`
	Hostname	string		`json:"hostname"`
	managed		bool		`json:"managed"`
	osi		Osi		`json:"osi"`
	bzi		Bzi		`json:"bzi"`
	plabel		string		`json:"plabel"`
	ptemplate	Ptemplate	`json:"ptemplate"`
}

