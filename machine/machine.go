package machine

import (
	"github.com/safl/bty/osi"
	"github.com/safl/bty/bzi"
	"github.com/safl/bty/pxe"
)

type Machine struct {
	Hwa		string		`json:"hwa"`
	Hostname	string		`json:"hostname"`
	Managed		bool		`json:"managed"`
	Osi		osi.Osi		`json:"osi"`
	Bzi		bzi.Bzi		`json:"bzi"`
	Plabel		string		`json:"plabel"`
	Ptemplate	pxe.Ptemplate	`json:"ptemplate"`
}

