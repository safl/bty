package pxe

import (
	"strings"
	"log"
	"github.com/safl/bty/conf"
	"github.com/safl/bty/finf"
)

type Pconfig struct {
	Finf	finf.Finf	`json:"finf"`
}

type Ptemplate struct {
	Finf	finf.Finf	`json:"finf"`

	Plabels	[]string	`json:"plabels"`
}

//
// On the given Ptemplate `tmpl` fill populate the list of labels
//
func annotate_labels(tmpl Ptemplate) error {

	for _, line := range strings.Split(string(tmpl.Finf.Content), "\n") {
		log.Printf("%s", line)
	}

	return nil
}

// Load PXE Configuration templates
func LoadPtemplates(cfg conf.Conf, ptemplates *[]Ptemplate, flags int) {
	for _, finf := range finf.FinfLoad(
		cfg.Locs.Ptemplates,
		cfg.Patterns.PtemplateExt,
		finf.FINF_CHECKSUM | finf.FINF_CONTENT,
	) {
		tmpl := Ptemplate{Finf: finf}
		err := annotate_labels(tmpl)
		if err != nil {
			log.Panic("failed annotating labels")
			continue
		}

		*ptemplates = append(*ptemplates, tmpl)
	}
}

// Load PXE Configuration files
func LoadPconfigs(cfg conf.Conf, pconfigs *[]Pconfig, flags int) {
	for _, finf := range finf.FinfLoad(
		cfg.Locs.Pconfigs,
		cfg.Patterns.PconfigExt,
		finf.FINF_CHECKSUM | finf.FINF_CONTENT,
	) {
		*pconfigs = append(*pconfigs, Pconfig{
			Finf: finf,
		})
	}
}

