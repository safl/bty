package osi

import (
	"strings"
	"fmt"
	"log"
	"github.com/safl/bty/conf"
	"github.com/safl/bty/finf"
)

type Osi struct {
	Finf	finf.Finf	`json:"finf"`
}

// Load Operating System Disk Images
func Load(cfg conf.Conf, osis []Osi, flags uint8) []Osi {
	do_checksum := flags & finf.FINF_CHECKSUM == 1

	flags = flags ^ finf.FINF_CHECKSUM

	for _, osi_finf := range finf.FinfLoad(
		cfg.Locs.Osis,
		cfg.Patterns.OsiExt,
		flags,
	) {
		if (!do_checksum) {
			osis = append(osis, Osi{Finf: osi_finf})
			continue
		}

		md5_fpath := fmt.Sprintf(
			"%s/%s.md5",
			cfg.Locs.Osis,
			osi_finf.Name,
		)
		md5_finf, err := finf.FinfStat(md5_fpath, finf.FINF_CONTENT)
		if err != nil {
			log.Fatal("stat failed, err: %v", err)
			continue
		}
		
		osi_finf.Checksum = strings.Split(string(md5_finf.Content), " ")[0]
		osis = append(osis, Osi{Finf: osi_finf})
	}

	return osis
}

