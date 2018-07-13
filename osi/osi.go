package osi

import (
	"github.com/safl/bty/conf"
	"github.com/safl/bty/finf"
)

type Osi struct {
	Finf	finf.Finf	`json:"finf"`
}

// Load Operating System Disk Images
func Load(cfg conf.Conf, osis []Osi, flags int) []Osi {

	// TODO: load checksum via .md5 file
	//	 remove from flags and handle here instead of by default method

	for _, osi_finf := range finf.FinfLoad(
		cfg.Locs.Osis,
		cfg.Patterns.OsiExt,
		0x0,
	) {
		osis = append(osis, Osi{Finf: osi_finf})
	}

	return osis
}

