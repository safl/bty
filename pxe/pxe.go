package pxe

import (
	"strings"
	"log"
	. "github.com/safl/bty/conf"
	. "github.com/safl/bty/finf"
)

type Pconfig struct {
	Finf	Finf	`json:"finf"`
}

type Ptemplate struct {
	Finf	Finf	`json:"finf"`

	Plabels	[]string	`json:"plabels"`
}

//
// On the given Ptemplate `tmpl` fill the labels attribute
//
func annotate_labels(tmpl Ptemplate) error {

	for _, line := range strings.Split(string(tmpl.Finf.Content), "\n") {
		log.Printf("%s", line)
	}

	return nil
}

// Load PXE Configuration templates
func LoadPtemplates(cfg Conf, ptemplates *[]Ptemplate, flags int) {
	for _, finf := range FinfLoad(
		cfg.Locs.Ptemplates,
		cfg.Patterns.PtemplateExt,
		FINF_CHECKSUM | FINF_CONTENT,
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
func LoadPconfigs(cfg Conf, pconfigs *[]Pconfig, flags int) {
	for _, finf := range FinfLoad(
		cfg.Locs.Pconfigs,
		cfg.Patterns.PconfigExt,
		FINF_CHECKSUM | FINF_CONTENT,
	) {
		*pconfigs = append(*pconfigs, Pconfig{
			Finf: finf,
		})
	}
}

