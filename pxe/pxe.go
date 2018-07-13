package pxe

import (
	"strings"
	"regexp"
	"log"
	"github.com/safl/bty/conf"
	"github.com/safl/bty/finf"
)

// PXE Configuration file
type Pconfig struct {
	Finf	finf.Finf	`json:"finf"`
}

// PXE Configuration file template
type Ptemplate struct {
	Finf	finf.Finf	`json:"finf"`

	Plabels	[]string	`json:"plabels"`
}

//
// On the given Ptemplate `tmpl` fill populate the list of labels
//
func annotate_labels(cfg conf.Conf, tmpl *Ptemplate) error {

	re := regexp.MustCompile(cfg.Patterns.PtemplateLbl)

	for _, line := range strings.Split(string(tmpl.Finf.Content), "\n") {
		match := re.FindStringSubmatch(line)
		if len(match) != 2 {
			continue
		}
		
		tmpl.Plabels = append(tmpl.Plabels, match[1])
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
		err := annotate_labels(cfg, &tmpl)
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

