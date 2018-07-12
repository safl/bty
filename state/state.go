package state

import (
	"path/filepath"
	"time"
	"log"
	"os"
	. "github.com/safl/bty/args"
)

type Finf struct {
	Flags		uint8		`json:"flags"`
	Name		string		`json:"name"`
	Size		int64		`json:"size"`
	Mode		os.FileMode	`json:"mode"`
	ModTime		time.Time	`json:"mod_time"`

	Checksum	string		`json:"checksum"`
	Content		string		`json:"content"`
}

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
	Config		Config		`json:"config"`

	Osis		[]Osi		`json:"osis"`
	Bzis		[]Bzi		`json:"bzis"`
	Pconfigs	[]Pconfig	`json:"pconfigs"`
	Ptemplates	[]Ptemplate	`json:"ptemplates"`
	machines	[]Machine	`json:"machines"`
}

const (
	FINF_CHECKSUM uint8 = 1 << iota
	FINF_CONTENT
)

func FinfStat(fpath string, flags uint8) (Finf, error) {

	finf := Finf{Flags: flags};

	info, err := os.Stat(fpath)
	if err != nil {
		log.Printf("err: %v", err)
		return finf, err
	}

	finf.Name = info.Name()
	finf.Size = info.Size()
	finf.Mode = info.Mode()
	finf.ModTime = info.ModTime()

	if (flags & FINF_CHECKSUM != 0) {
		// TODO: implement checksum calculation
	}

	if (flags & FINF_CONTENT != 0) {
		// TODO: implement content load
	}

	return finf, nil
}

func FinfLoad(dpath string, glob string, flags uint8) []Finf {

	finfs := []Finf{}

	var fpaths, err = filepath.Glob(dpath + glob)
	if err != nil {
		log.Printf("filepath.Glob failed with err: %v", err)
		return finfs
	}

	for _, fpath := range fpaths {
		log.Printf("fpath: %s", fpath)
		finf, err := FinfStat(fpath, flags)
		if err != nil {
			log.Printf("skipping fpath: %s due to err", fpath)
			continue
		}

		finfs = append(finfs, finf)
	}

	return finfs
}

// Load Operating System Disk Images
func LoadOsis(cfg Config, osis *[]Osi, flags int) {

	finfs := FinfLoad(
		cfg.Locs.Osis,
		cfg.Patterns.OsiExt,
		0x0,
	)
	for _, finf := range finfs {
		*osis = append(*osis, Osi{
			Finf: finf,
		})

		// TODO: load checksum via .md5 file

		// TODO: load content
	}
}

// Load Operating System Disk Images
func LoadBzis(cfg Config, bzis *[]Bzi, flags int) {

	finfs := FinfLoad(
		cfg.Locs.Bzis,
		cfg.Patterns.BziExt,
		FINF_CHECKSUM,
	)
	for _, finf := range finfs {
		*bzis = append(*bzis, Bzi{
			Finf: finf,
		})
	}
}

// Load Operating System Disk Images
func LoadPconfigs(cfg Config, pconfigs *[]Pconfig, flags int) {

	finfs := FinfLoad(
		cfg.Locs.Pconfigs,
		cfg.Patterns.PconfigExt,
		FINF_CHECKSUM | FINF_CONTENT,
	)
	for _, finf := range finfs {
		*pconfigs = append(*pconfigs, Pconfig{
			Finf: finf,
		})
	}
}

// Load Operating System Disk Images
func LoadPtemplates(cfg Config, ptemplates *[]Ptemplate, flags int) {

	finfs := FinfLoad(
		cfg.Locs.Ptemplates,
		cfg.Patterns.PtemplateExt,
		FINF_CHECKSUM | FINF_CONTENT,
	)
	for _, finf := range finfs {
		*ptemplates = append(*ptemplates, Ptemplate{
			Finf: finf,
		})
	}

}

