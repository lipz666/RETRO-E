from __future__ import annotations

from dataclasses import dataclass


class ChemistryDependencyError(RuntimeError):
    pass


def _chem():
    try:
        from rdkit import Chem, RDLogger
    except ImportError as exc:
        raise ChemistryDependencyError(
            "RDKit is required for experiment execution. Install the project in Python 3.11-3.13."
        ) from exc
    RDLogger.DisableLog("rdApp.*")
    return Chem


def molecule(smiles: str):
    if not isinstance(smiles, str) or not smiles.strip():
        return None
    chem = _chem()
    try:
        return chem.MolFromSmiles(smiles.strip())
    except Exception:  # noqa: BLE001 -- RDKit may raise Boost.Python exception types
        return None


def canonicalize(smiles: str) -> str | None:
    mol = molecule(smiles)
    if mol is None:
        return None
    return _chem().MolToSmiles(mol, canonical=True)


def heavy_atoms(smiles: str) -> int:
    mol = molecule(smiles)
    return mol.GetNumHeavyAtoms() if mol is not None else 0


def ring_count(smiles: str) -> int:
    mol = molecule(smiles)
    return mol.GetRingInfo().NumRings() if mol is not None else 0


def murcko_scaffold(smiles: str) -> str | None:
    mol = molecule(smiles)
    if mol is None:
        return None
    try:
        from rdkit.Chem.Scaffolds import MurckoScaffold

        scaffold = MurckoScaffold.GetScaffoldForMol(mol)
        return _chem().MolToSmiles(scaffold, canonical=True) if scaffold.GetNumAtoms() else ""
    except Exception:  # noqa: BLE001 -- scaffold generation has extension-defined errors
        return None


@dataclass(frozen=True)
class MoleculeCheck:
    raw: str
    canonical: str | None
    valid: bool
    heavy_atom_count: int


def check_molecule(smiles: str) -> MoleculeCheck:
    canonical = canonicalize(smiles)
    return MoleculeCheck(
        raw=smiles,
        canonical=canonical,
        valid=canonical is not None,
        heavy_atom_count=heavy_atoms(canonical) if canonical else 0,
    )
