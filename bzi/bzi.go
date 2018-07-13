package bzi

import (
	"github.com/safl/bty/conf"
	"github.com/safl/bty/finf"
)

type Bzi struct {
	Finf	finf.Finf	`json:"finf"`
}

// Load Operating System Disk Images
func Load(cfg conf.Conf, bzis []Bzi, flags int) []Bzi {

	for _, bzi_finf := range finf.FinfLoad(
		cfg.Locs.Bzis,
		cfg.Patterns.BziExt,
		finf.FINF_CHECKSUM,
	) {
		bzis = append(bzis, Bzi{Finf: bzi_finf})
	}

	return bzis
}

