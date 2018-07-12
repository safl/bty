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

	osi.Load(cfg, &curs.Osis, 0x0)
	bzi.Load(cfg, &curs.Bzis, 0x0)
	pxe.LoadPconfigs(cfg, &curs.Pconfigs, 0x0)
	pxe.LoadPtemplates(cfg, &curs.Ptemplates, 0x0)

	return curs, nil
}

