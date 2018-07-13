package state

import (
	"github.com/safl/bty/conf"
	"github.com/safl/bty/pxe"
	"github.com/safl/bty/osi"
	"github.com/safl/bty/bzi"
	"github.com/safl/bty/machine"
)

type State struct {
	Conf		conf.Conf		`json:"config"`

	Osis		[]osi.Osi		`json:"osis"`
	Bzis		[]bzi.Bzi		`json:"bzis"`
	Pconfigs	[]pxe.Pconfig		`json:"pconfigs"`
	Ptemplates	[]pxe.Ptemplate		`json:"ptemplates"`
	machines	[]machine.Machine	`json:"machines"`
}

// Initialize the state of BTY using the given configuration
func Initialize(cfg conf.Conf) (State, error) {

	curs := State{Conf: cfg}
	curs.Osis = osi.Load(cfg, curs.Osis, 0x0)
	curs.Bzis = bzi.Load(cfg, curs.Bzis, 0x0)
	curs.Pconfigs = pxe.LoadPconfigs(cfg, curs.Pconfigs, 0x0)
	curs.Ptemplates = pxe.LoadPtemplates(cfg, curs.Ptemplates, 0x0)

	return curs, nil
}

