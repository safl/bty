package state

import (
	. "github.com/safl/bty/conf"
	. "github.com/safl/bty/finf"
)

type Osi struct {
	Finf	Finf	`json:"finf"`
}

type Bzi struct {
	Finf	Finf	`json:"finf"`
}

type Pconfig struct {
	Finf	Finf	`json:"finf"`
}

type Ptemplate struct {
	Finf	Finf	`json:"finf"`

	Plabels	[]string	`json:"plabels"`
}

type Machine struct {
	hwa		string		`json:"hwa"`
	Hostname	string		`json:"hostname"`
	managed		bool		`json:"managed"`
	osi		Osi		`json:"osi"`
	bzi		Bzi		`json:"bzi"`
	plabel		string		`json:"plabel"`
	ptemplate	Ptemplate	`json:"ptemlate"`
}

type State struct {
	Conf		Conf		`json:"config"`

	Osis		[]Osi		`json:"osis"`
	Bzis		[]Bzi		`json:"bzis"`
	Pconfigs	[]Pconfig	`json:"pconfigs"`
	Ptemplates	[]Ptemplate	`json:"ptemplates"`
	machines	[]Machine	`json:"machines"`
}

// Load Operating System Disk Images
func LoadOsis(cfg Conf, osis *[]Osi, flags int) {

	// TODO: load checksum via .md5 file
	//	 remove from flags and handle here instead of by default method

	for _, finf := range FinfLoad(
		cfg.Locs.Osis,
		cfg.Patterns.OsiExt,
		0x0,
	) {
		*osis = append(*osis, Osi{
			Finf: finf,
		})
	}
}

// Load Operating System Disk Images
func LoadBzis(cfg Conf, bzis *[]Bzi, flags int) {

	for _, finf := range FinfLoad(
		cfg.Locs.Bzis,
		cfg.Patterns.BziExt,
		FINF_CHECKSUM,
	) {
		*bzis = append(*bzis, Bzi{
			Finf: finf,
		})
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

// Load PXE Configuration templates
func LoadPtemplates(cfg Conf, ptemplates *[]Ptemplate, flags int) {

	for _, finf := range FinfLoad(
		cfg.Locs.Ptemplates,
		cfg.Patterns.PtemplateExt,
		FINF_CHECKSUM | FINF_CONTENT,
	) {
		*ptemplates = append(*ptemplates, Ptemplate{
			Finf: finf,
		})

		// TODO: load labels
	}

}

// Initialize the state of BTY using the given configuration
func Initialize(cfg Conf) (State, error) {

	curs := State{Conf: cfg}

	LoadOsis(cfg, &curs.Osis, 0x0)
	LoadBzis(cfg, &curs.Bzis, 0x0)
	LoadPconfigs(cfg, &curs.Pconfigs, 0x0)
	LoadPtemplates(cfg, &curs.Ptemplates, 0x0)

	return curs, nil
}