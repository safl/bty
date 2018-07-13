package pxe

import (
	"strings"
	"regexp"
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

// Fills `labels` with PXE LABELs found in the given `content` using Pattern in
// the passed cfg
func find_labels(cfg conf.Conf, content []byte, labels []string) []string {
	re := regexp.MustCompile(cfg.Patterns.PtemplateLbl)

	for _, line := range strings.Split(string(content), "\n") {
		match := re.FindStringSubmatch(line)
		if len(match) != 2 {
			continue
		}
		
		labels = append(labels, match[1])
	}

	return labels
}

// Load PXE Configuration templates
func LoadPtemplates(cfg conf.Conf, ptemplates []Ptemplate, flags int) []Ptemplate {
	for _, finf := range finf.FinfLoad(
		cfg.Locs.Ptemplates,
		cfg.Patterns.PtemplateExt,
		finf.FINF_CHECKSUM | finf.FINF_CONTENT,
	) {
		tmpl := Ptemplate{ Finf: finf }

		tmpl.Plabels = find_labels(cfg, tmpl.Finf.Content, tmpl.Plabels)

		ptemplates = append(ptemplates, tmpl)
	}

	return ptemplates
}

// Load PXE Configuration files
func LoadPconfigs(cfg conf.Conf, pconfigs []Pconfig, flags int) []Pconfig {
	for _, finf := range finf.FinfLoad(
		cfg.Locs.Pconfigs,
		cfg.Patterns.PconfigExt,
		finf.FINF_CHECKSUM | finf.FINF_CONTENT,
	) {
		pconfigs = append(pconfigs, Pconfig{
			Finf: finf,
		})
	}

	return pconfigs
}

