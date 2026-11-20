"""Behavioural coverage for MonthCloseOrchestrator paths not hit by test_orchestrator.py.

Every external dependency (storage, Fakturownia, KSeF, Zoho, Canva, reports
download, secrets) is mocked at the orchestrator module boundary.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.client import DownloadedCostDocument
from zdrovena.month_closing.config import VendorConfig
from zdrovena.month_closing.orchestrator import MonthCloseOrchestrator

MOD = "zdrovena.month_closing.orchestrator"


def _orch(tmp_path: Path, *, dry_run: bool = False, **kwargs) -> MonthCloseOrchestrator:
    orch = MonthCloseOrchestrator(year=2025, month=6, dry_run=dry_run, **kwargs)
    orch.out = MagicMock()
    orch.storage = MagicMock()
    orch.storage.list_files.return_value = []
    orch.state = MagicMock()
    orch.state.is_done.return_value = False
    orch.month_dir = tmp_path / "month"
    orch.sales_dir = orch.month_dir / "sprzedaz"
    orch.costs_dir = orch.month_dir / "koszty"
    orch.decl_dir = orch.month_dir / "deklaracje"
    orch.ksef_enabled = False
    return orch


def _blob(key: str) -> SimpleNamespace:
    return SimpleNamespace(key=key)


# ── Blob helpers ────────────────────────────────────────────────────────────


class TestBlobHelpers:
    def test_upload_to_blob_uploads_under_dir_key(self, tmp_path):
        orch = _orch(tmp_path)
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        orch._upload_to_blob(f, "pre/fix")
        orch.storage.upload.assert_called_once_with(f, "pre/fix/a.pdf")

    def test_upload_to_blob_swallows_storage_errors(self, tmp_path):
        orch = _orch(tmp_path)
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        orch.storage.upload.side_effect = OSError("down")
        orch._upload_to_blob(f, "pre")  # must not raise

    def test_upload_to_blob_noop_for_missing_file_and_dry_run(self, tmp_path):
        orch = _orch(tmp_path)
        orch._upload_to_blob(tmp_path / "missing.pdf", "pre")
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        orch.dry_run = True
        orch._upload_to_blob(f, "pre")
        orch.storage.upload.assert_not_called()

    def test_upload_dir_uploads_files_only(self, tmp_path):
        orch = _orch(tmp_path)
        d = tmp_path / "d"
        (d / "sub").mkdir(parents=True)
        (d / "one.pdf").write_bytes(b"1")
        (d / "two.pdf").write_bytes(b"2")
        orch._upload_dir_to_blob(d, "k")
        keys = sorted(c.args[1] for c in orch.storage.upload.call_args_list)
        assert keys == ["k/one.pdf", "k/two.pdf"]

    def test_upload_dir_noop_when_missing(self, tmp_path):
        orch = _orch(tmp_path)
        orch._upload_dir_to_blob(tmp_path / "nope", "k")
        orch.storage.upload.assert_not_called()

    def test_blob_file_exists_matches_suffix_and_exact(self, tmp_path):
        orch = _orch(tmp_path)
        orch.storage.list_files.return_value = [_blob("p/sub/x.pdf")]
        assert orch._blob_file_exists("p", "x.pdf") is True
        orch.storage.list_files.assert_called_with("p/")
        orch.storage.list_files.return_value = [_blob("p/y.pdf")]
        assert orch._blob_file_exists("p", "y.pdf") is True
        assert orch._blob_file_exists("p", "z.pdf") is False

    def test_blob_file_exists_false_on_storage_error(self, tmp_path):
        orch = _orch(tmp_path)
        orch.storage.list_files.side_effect = RuntimeError("boom")
        assert orch._blob_file_exists("p", "x.pdf") is False

    def test_mark_step_done_persists_in_live_mode(self, tmp_path):
        orch = _orch(tmp_path)
        orch._mark_step_done("Step")
        orch.state.mark_done.assert_called_once_with("Step")

    def test_skip_if_done_ignores_state_when_not_managed(self, tmp_path):
        orch = _orch(tmp_path, manage_state=False)
        orch.state.is_done.return_value = True
        assert orch._skip_if_done("X") is False


# ── Execution modes ─────────────────────────────────────────────────────────


class TestExecuteStage:
    def _stub(self, orch):
        for name in (
            "_step_1_create_folders",
            "_step_2_sales_invoices",
            "_step_3_jpk_reports",
            "_step_4_cost_invoices",
            "_step_5_bank_statement",
            "_step_6_zip_archive",
            "_step_7_email",
            "_load_existing_zip",
            "_prepare_uploaded_inputs",
            "_print_summary",
        ):
            setattr(orch, name, MagicMock())

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            (" Sales ", ["_step_1_create_folders", "_step_2_sales_invoices"]),
            ("costs", ["_prepare_uploaded_inputs", "_step_4_cost_invoices"]),
            ("reports", ["_prepare_uploaded_inputs", "_step_3_jpk_reports"]),
            ("bank", ["_prepare_uploaded_inputs", "_step_5_bank_statement"]),
            ("package", ["_step_6_zip_archive"]),
            ("send", ["_load_existing_zip", "_step_7_email"]),
        ],
    )
    def test_stage_dispatches_to_its_steps(self, tmp_path, stage, expected):
        orch = _orch(tmp_path)
        self._stub(orch)
        orch.execute_stage(stage)
        for name in expected:
            getattr(orch, name).assert_called_once()
        orch._print_summary.assert_called_once()

    def test_unknown_stage_raises_and_records_error(self, tmp_path):
        orch = _orch(tmp_path)
        self._stub(orch)
        with pytest.raises(ValueError, match="Unknown month-close stage"):
            orch.execute_stage("bogus")
        assert any("bogus" in e for e in orch.report.errors)
        orch._print_summary.assert_called_once()

    @patch(f"{MOD}.PreflightChecker")
    def test_prepare_uploaded_inputs_runs_preflight_without_gate(self, checker_cls, tmp_path):
        orch = _orch(tmp_path)
        checker = MagicMock()
        checker.result.bank_statement_found = True
        checker_cls.return_value = checker
        orch._prepare_uploaded_inputs()
        checker.run.assert_called_once()
        checker.build_blockers.assert_not_called()
        checker.copy_to_folders.assert_called_once_with(orch.month_dir, orch.costs_dir)
        assert orch.report.bank_statement_found is True
        assert orch.costs_dir.exists()


class TestExecuteErrorPaths:
    def test_zip_only_records_error_and_reraises(self, tmp_path):
        orch = _orch(tmp_path)
        orch._step_0_preflight = MagicMock(side_effect=RuntimeError("pf failed"))
        orch._print_summary = MagicMock()
        with pytest.raises(RuntimeError):
            orch.execute_zip_only()
        assert orch.report.errors == ["pf failed"]
        orch._print_summary.assert_called_once()

    def test_zip_and_send_records_error_and_reraises(self, tmp_path):
        orch = _orch(tmp_path)
        orch._step_6_zip_archive = MagicMock(side_effect=RuntimeError("zip failed"))
        orch._print_summary = MagicMock()
        with pytest.raises(RuntimeError):
            orch.execute_zip_and_send()
        assert orch.report.errors == ["zip failed"]

    def test_send_only_reports_unrestored_zip_path(self, tmp_path):
        orch = _orch(tmp_path)
        orch._load_existing_zip = MagicMock()  # leaves zip_path None
        orch._step_7_email = MagicMock()
        orch._print_summary = MagicMock()
        report = orch.execute_send_only()
        assert report.errors == ["ZIP path was not restored"]
        orch._step_7_email.assert_not_called()

    def test_load_existing_zip_falls_back_to_blob(self, tmp_path):
        orch = _orch(tmp_path)
        orch.storage.exists.return_value = True
        orch._load_existing_zip()
        assert orch.report.zip_path == Path(f"{orch._blob_prefix}/czerwiec_2025_HUMIO.zip")


# ── Step 0 / 2 / 3 extra branches ───────────────────────────────────────────


class TestStep0IgnoreWarnings:
    @patch(f"{MOD}.PreflightChecker")
    def test_blockers_become_warnings(self, checker_cls, tmp_path):
        orch = _orch(tmp_path, ignore_warnings=True)
        checker = MagicMock()
        checker.run.return_value = SimpleNamespace(bank_statement_found=False, warnings=[])
        checker.build_blockers.return_value = ["  Missing: bank.pdf  "]
        checker_cls.return_value = checker
        orch._step_0_preflight()
        assert "Missing: bank.pdf" in orch.report.warnings
        assert "Pre-flight" in orch.report.steps_completed


class TestStep2Skip:
    @patch(f"{MOD}.FakturowniaClient")
    def test_already_done_skips_fetch(self, client_cls, tmp_path):
        orch = _orch(tmp_path)
        orch.state.is_done.return_value = True
        orch._step_2_sales_invoices()
        client_cls.from_keyring.assert_not_called()


class TestStep3Download:
    REPORTS: ClassVar[list[dict[str, str]]] = [
        {"name": "JPK_FA", "dest_name": "JPK_FA.xml"},
        {"name": "VAT", "dest_name": "VAT.pdf"},
    ]

    def test_missing_reports_are_auto_downloaded_and_uploaded(self, tmp_path):
        orch = _orch(tmp_path)
        orch.decl_dir.mkdir(parents=True)
        path = orch.decl_dir / "JPK_FA.xml"

        def fake_download(missing, date_from, date_to, dest_dir):
            assert [r["name"] for r in missing] == ["JPK_FA", "VAT"]
            assert (date_from, date_to, dest_dir) == ("2025-06-01", "2025-06-30", orch.decl_dir)
            path.write_bytes(b"<x/>")
            return [(self.REPORTS[0], path)]

        with (
            patch(f"{MOD}.FAKTUROWNIA_REPORTS", self.REPORTS),
            patch(f"{MOD}.download_fakturownia_reports", side_effect=fake_download),
        ):
            orch._step_3_jpk_reports()
        orch.storage.upload.assert_called_once_with(
            path, f"{orch._blob_prefix}/deklaracje/JPK_FA.xml"
        )
        assert any("VAT" in w and "incomplete" in w for w in orch.report.warnings)
        assert "JPK & VAT reports" not in orch.report.steps_completed

    def test_download_failure_is_warning_only(self, tmp_path):
        orch = _orch(tmp_path)
        with (
            patch(f"{MOD}.FAKTUROWNIA_REPORTS", self.REPORTS[:1]),
            patch(f"{MOD}.download_fakturownia_reports", side_effect=RuntimeError("no browser")),
        ):
            orch._step_3_jpk_reports()
        assert any("incomplete" in w for w in orch.report.warnings)

    def test_report_in_month_root_is_moved_to_deklaracje(self, tmp_path):
        orch = _orch(tmp_path, non_interactive=True)
        orch.decl_dir.mkdir(parents=True)
        (orch.month_dir / "JPK_FA.xml").write_bytes(b"<x/>")
        with patch(f"{MOD}.FAKTUROWNIA_REPORTS", self.REPORTS[:1]):
            orch._step_3_jpk_reports()
        assert (orch.decl_dir / "JPK_FA.xml").exists()
        assert not (orch.month_dir / "JPK_FA.xml").exists()

    def test_report_present_false_when_listing_fails(self, tmp_path):
        orch = _orch(tmp_path)
        orch.storage.list_files.side_effect = RuntimeError("down")
        assert orch._report_present(self.REPORTS[0]) is False


# ── Step 4: cost invoices ───────────────────────────────────────────────────

SHOP = VendorConfig(name="Shop", pattern="shop", email="billing@shop")
STRICT = VendorConfig(name="Strict", pattern="strict", source_policy="original_required")
MANUAL = VendorConfig(name="Manual", pattern="manual", manual=True)
SKIPPED = VendorConfig(name="Skipped", pattern="skipped", skip=True)
CANVA = VendorConfig(
    name="Canva",
    pattern="canva",
    browser_download=True,
    invoice_id_re=r"(\d+)",
)


def _match(name, _nip):
    name = name.casefold()
    for v in (SHOP, STRICT, MANUAL, CANVA):
        if v.pattern in name:
            return v
    return None


def _inv(id_, buyer, *, sell_date="2025-06-10", attachments=False, gov_id=None, gross="10"):
    return {
        "id": id_,
        "buyer_name": buyer,
        "buyer_tax_no": "",
        "sell_date": sell_date,
        "number": f"N{id_}",
        "price_gross": gross,
        "has_attachments": attachments,
        "gov_id": gov_id,
    }


@pytest.fixture()
def cost_env(tmp_path):
    """Patch vendor registry, clients and secrets; yield handles for each test."""
    fakt = MagicMock()
    fakt.fetch_cost_invoices.return_value = []
    fakt.download_cost_documents.return_value = []
    fakt.cost_document_stem.side_effect = lambda inv: f"gen_{inv['id']}"
    zoho = MagicMock()
    zoho.search_and_download_vendor.return_value = {"found": False}
    zoho.extract_invoice_ids.return_value = []
    ksef = MagicMock()
    ksef.query_purchase_invoices.return_value = []
    secrets = {"client_id": "a", "client_secret": "b", "refresh_token": "c"}
    with (
        patch(f"{MOD}.EXPECTED_VENDORS", [SHOP, STRICT, MANUAL, SKIPPED, CANVA]),
        patch(f"{MOD}.match_vendor", side_effect=_match),
        patch(f"{MOD}.FakturowniaClient") as fakt_cls,
        patch(f"{MOD}.ZohoMailClient", return_value=zoho) as zoho_cls,
        patch(f"{MOD}.KSeFClient", return_value=ksef),
        patch(f"{MOD}.download_canva_invoice") as canva_dl,
        patch(f"{MOD}.validate_invoice_dates") as validate,
        patch(f"{MOD}.delete_rejected") as delete_rejected,
        patch(f"{MOD}.move_unverified") as move_unverified,
        patch.object(
            MonthCloseOrchestrator,
            "_get_secret",
            side_effect=lambda svc, required=True: secrets.get(svc),
        ),
        patch(f"{MOD}.KEYCHAIN_SERVICE_ZOHO_CLIENT_ID", "client_id"),
        patch(f"{MOD}.KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET", "client_secret"),
        patch(f"{MOD}.KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN", "refresh_token"),
    ):
        fakt_cls.from_keyring.return_value = fakt
        validate.return_value = ([], [], [])
        yield SimpleNamespace(
            fakt=fakt,
            zoho=zoho,
            zoho_cls=zoho_cls,
            ksef=ksef,
            canva_dl=canva_dl,
            validate=validate,
            delete_rejected=delete_rejected,
            move_unverified=move_unverified,
            secrets=secrets,
        )


class TestStep4CostInvoices:
    def test_skips_when_already_done(self, tmp_path, cost_env):
        orch = _orch(tmp_path)
        orch.state.is_done.return_value = True
        orch._step_4_cost_invoices()
        cost_env.fakt.fetch_cost_invoices.assert_not_called()

    def test_ksef_cross_check_warns_about_missing_invoices(self, tmp_path, cost_env):
        orch = _orch(tmp_path)
        orch.ksef_enabled = True
        cost_env.ksef.query_purchase_invoices.return_value = [
            {"ksefNumber": "K1", "seller": {"name": "S"}},
            {"ksefNumber": "K2"},
            {"seller": {"name": "no number"}},
        ]
        cost_env.fakt.fetch_cost_invoices.return_value = [_inv(1, "Other", gov_id="K1")]
        cost_env.secrets.clear()  # Zoho not configured
        orch._step_4_cost_invoices()
        cost_env.ksef.authenticate.assert_called_once()
        assert orch.report.ksef_count == 3
        assert any("K2" in w and "missing in Fakturownia" in w for w in orch.report.warnings)
        assert not any("K1," in w for w in orch.report.warnings)
        cost_env.zoho_cls.assert_not_called()

    def test_live_original_attachment_replaces_generated_copy(self, tmp_path, cost_env):
        orch = _orch(tmp_path)
        orch.costs_dir.mkdir(parents=True)
        generated = orch.costs_dir / "gen_1.pdf"
        generated.write_bytes(b"gen")
        invoices = [
            _inv(1, "Shop Inc", attachments=True),
            _inv(2, "Strict Ltd", attachments=False),  # original required, none → Zoho
            _inv(3, "Unknown Co"),
            _inv(4, "Shop Inc", sell_date="2025-07-03"),  # overlap window only
            _inv(5, "Shop Inc"),  # original_attachment for an invoice not in downloadable
        ]
        cost_env.fakt.fetch_cost_invoices.return_value = invoices
        cost_env.fakt.download_cost_documents.return_value = [
            DownloadedCostDocument(Path("o1.pdf"), 1, "N1", "Shop", "original_attachment"),
            DownloadedCostDocument(Path("g3.pdf"), 3, "N3", "?", "generated_pdf"),
            DownloadedCostDocument(Path("x.pdf"), 99, "N99", "?", "original_attachment"),
        ]
        orch.storage.delete.side_effect = FileNotFoundError("gone")
        cost_env.secrets.clear()

        orch._step_4_cost_invoices()

        downloadable = cost_env.fakt.download_cost_documents.call_args.args[0]
        assert [i["id"] for i in downloadable] == [1, 3, 5]
        policy = cost_env.fakt.download_cost_documents.call_args.kwargs["source_policy"]
        assert policy(invoices[0]) == "original_preferred"
        assert policy(invoices[2]) == "original_preferred"
        assert not generated.exists()
        orch.storage.delete.assert_called_once_with(f"{orch._blob_prefix}/koszty/gen_1.pdf")
        assert orch.report.cost_found_vendors == {"Shop": "Fakturownia — oryginalny załącznik"}
        assert orch.report.cost_invoice_count == 3
        assert orch.report.cost_missing_vendors == ["Strict", "Manual", "Canva"]
        assert "Cost invoices" in orch.report.steps_completed

    def test_generated_pdf_source_is_labelled(self, tmp_path, cost_env):
        orch = _orch(tmp_path)
        cost_env.fakt.fetch_cost_invoices.return_value = [_inv(1, "Shop Inc")]
        cost_env.fakt.download_cost_documents.return_value = [
            DownloadedCostDocument(Path("g.pdf"), 1, "N1", "Shop", "generated_pdf"),
        ]
        cost_env.secrets.clear()
        orch._step_4_cost_invoices()
        assert orch.report.cost_found_vendors["Shop"] == "Fakturownia — wygenerowany PDF"

    def test_dry_run_labels_vendors_from_downloadable(self, tmp_path, cost_env):
        orch = _orch(tmp_path, dry_run=True)
        cost_env.fakt.fetch_cost_invoices.return_value = [
            _inv(1, "Shop Inc", attachments=True),
            _inv(2, "Strict Ltd", attachments=True),
            _inv(3, "Unknown"),
        ]
        cost_env.secrets.clear()
        orch._step_4_cost_invoices()
        assert orch.report.cost_found_vendors == {
            "Shop": "Fakturownia — oryginalny załącznik",
            "Strict": "Fakturownia — oryginalny załącznik",
        }
        assert orch.report.cost_invoice_count == 3
        orch.state.mark_done.assert_not_called()

    def test_dry_run_generated_label(self, tmp_path, cost_env):
        orch = _orch(tmp_path, dry_run=True)
        cost_env.fakt.fetch_cost_invoices.return_value = [_inv(1, "Shop Inc")]
        cost_env.secrets.clear()
        orch._step_4_cost_invoices()
        assert orch.report.cost_found_vendors["Shop"] == "Fakturownia — wygenerowany PDF"

    def test_preflight_matches_count_as_found(self, tmp_path, cost_env):
        orch = _orch(tmp_path)
        orch._preflight_checker = SimpleNamespace(
            result=SimpleNamespace(
                matches=[
                    (SHOP, Path("a.pdf")),
                    ({"name": "Strict"}, Path("b.pdf")),
                    (SHOP, Path("dup.pdf")),
                ]
            )
        )
        orch._step_4_cost_invoices()
        assert orch.report.cost_found_vendors["Shop"] == "Pre-flight (manual)"
        assert orch.report.cost_found_vendors["Strict"] == "Pre-flight (manual)"
        assert orch.report.cost_invoice_count == 2

    def test_all_found_skips_zoho(self, tmp_path, cost_env):
        orch = _orch(tmp_path, ignore_vendors=["Canva", "Manual"])
        orch._preflight_checker = SimpleNamespace(
            result=SimpleNamespace(matches=[(SHOP, Path("a")), (STRICT, Path("b"))])
        )
        orch._step_4_cost_invoices()
        cost_env.zoho_cls.assert_not_called()
        assert orch.report.cost_missing_vendors == []
        assert not orch.report.warnings

    def test_zoho_finds_vendors_and_removes_generated_copies(self, tmp_path, cost_env):
        orch = _orch(tmp_path, ignore_vendors=["manual"])
        orch.costs_dir.mkdir(parents=True)
        (orch.costs_dir / "gen_2.pdf").write_bytes(b"gen")
        cost_env.fakt.fetch_cost_invoices.return_value = [
            _inv(1, "Other Co"),
            _inv(2, "Strict Ltd"),
        ]
        shop_pdf = orch.costs_dir / "shop.pdf"
        strict_pdf = orch.costs_dir / "strict.pdf"

        def search(vendor_name, **_):
            if vendor_name == "Shop":
                return {"found": True, "downloaded": 1, "saved_paths": [shop_pdf]}
            if vendor_name == "Strict":
                return {"found": True, "downloaded": 1, "saved_paths": [strict_pdf]}
            return {"found": False}

        cost_env.zoho.search_and_download_vendor.side_effect = search
        orch.storage.delete.side_effect = FileNotFoundError("none")
        cost_env.validate.return_value = ([shop_pdf, strict_pdf], [], [])

        orch._step_4_cost_invoices()

        cost_env.zoho.authenticate.assert_called_once()
        kwargs = cost_env.zoho.search_and_download_vendor.call_args_list[0].kwargs
        assert kwargs["search_term"] == "billing@shop"
        assert kwargs["date_from"] == "2025/06/01"
        strict_kwargs = cost_env.zoho.search_and_download_vendor.call_args_list[1].kwargs
        assert strict_kwargs["search_term"] == "strict"  # falls back to pattern
        assert not (orch.costs_dir / "gen_2.pdf").exists()
        orch.storage.delete.assert_called_once_with(f"{orch._blob_prefix}/koszty/gen_2.pdf")
        assert orch.report.cost_found_vendors["Shop"] == "Zoho Mail"
        assert orch.report.cost_found_vendors["Strict"] == "Zoho Mail"
        cost_env.validate.assert_called_once()
        assert cost_env.validate.call_args.kwargs == {"strict": False}

    def test_zoho_vendor_not_found_stays_missing(self, tmp_path, cost_env):
        orch = _orch(tmp_path, ignore_vendors=["canva", "manual"])
        orch._step_4_cost_invoices()
        assert orch.report.cost_missing_vendors == ["Shop", "Strict"]
        cost_env.validate.assert_not_called()
        assert any("Brak faktur kosztowych" in w for w in orch.report.warnings)

    def test_browser_vendor_download_paths(self, tmp_path, cost_env):
        orch = _orch(tmp_path, ignore_vendors=["shop", "strict", "manual"])
        orch.costs_dir.mkdir(parents=True)
        (orch.costs_dir / "invoice-111.pdf").write_bytes(b"existing")
        cost_env.zoho.extract_invoice_ids.return_value = [
            {"id": "111"},
            {"id": "222"},
            {"id": "333"},
        ]

        def canva(inv_id, dest):
            if inv_id == "333":
                raise RuntimeError("captcha")

        cost_env.canva_dl.side_effect = canva
        cost_env.validate.return_value = ([orch.costs_dir / "invoice-222.pdf"], [], [])

        orch._step_4_cost_invoices()

        assert [c.args[0] for c in cost_env.canva_dl.call_args_list] == ["222", "333"]
        assert cost_env.zoho.extract_invoice_ids.call_args.kwargs["search_term"] == "canva"
        assert orch.report.cost_found_vendors == {"Canva": "Zoho Mail + Browser"}
        assert orch.report.cost_invoice_count == 2
        validated = cost_env.validate.call_args.args[0]
        assert validated == [orch.costs_dir / "invoice-222.pdf"]

    def test_browser_vendor_dry_run_and_no_ids(self, tmp_path, cost_env):
        orch = _orch(tmp_path, dry_run=True, ignore_vendors=["shop", "strict", "manual"])
        cost_env.zoho.extract_invoice_ids.return_value = [{"id": "9"}]
        orch._step_4_cost_invoices()
        cost_env.canva_dl.assert_not_called()
        assert orch.report.cost_found_vendors == {"Canva": "Zoho Mail + Browser"}

        orch2 = _orch(tmp_path, ignore_vendors=["shop", "strict", "manual"])
        cost_env.zoho.extract_invoice_ids.return_value = []
        orch2._step_4_cost_invoices()
        assert orch2.report.cost_missing_vendors == ["Canva"]

    def test_browser_vendor_all_downloads_fail(self, tmp_path, cost_env):
        orch = _orch(tmp_path, ignore_vendors=["shop", "strict", "manual"])
        cost_env.zoho.extract_invoice_ids.return_value = [{"id": "1"}]
        cost_env.canva_dl.side_effect = RuntimeError("nope")
        orch._step_4_cost_invoices()
        assert "Canva" not in orch.report.cost_found_vendors
        cost_env.validate.assert_not_called()

    def test_candidate_gate_unmarks_vendor_whose_pdfs_all_fail(self, tmp_path, cost_env):
        orch = _orch(tmp_path, ignore_vendors=["canva", "manual"])
        shop_a = tmp_path / "shop_a.pdf"
        shop_b = tmp_path / "shop_b.pdf"
        strict_a = tmp_path / "strict_a.pdf"
        strict_b = tmp_path / "strict_b.pdf"

        def search(vendor_name, **_):
            paths = [shop_a, shop_b] if vendor_name == "Shop" else [strict_a, strict_b]
            return {"found": True, "downloaded": 2, "saved_paths": paths}

        cost_env.zoho.search_and_download_vendor.side_effect = search
        cost_env.validate.return_value = ([strict_a], [shop_a], [shop_b, strict_b])
        cost_env.delete_rejected.return_value = [shop_a]
        cost_env.move_unverified.return_value = [shop_b, strict_b]

        orch._step_4_cost_invoices()

        cost_env.delete_rejected.assert_called_once_with([shop_a])
        cost_env.move_unverified.assert_called_once_with([shop_b, strict_b])
        assert "Shop" not in orch.report.cost_found_vendors
        assert orch.report.cost_found_vendors["Strict"] == "Zoho Mail"
        assert orch.report.cost_invoice_count == 1
        assert orch.report.cost_missing_vendors == ["Shop"]

    def test_candidate_gate_skipped_in_dry_run(self, tmp_path, cost_env):
        orch = _orch(tmp_path, dry_run=True, ignore_vendors=["canva", "manual", "strict"])
        cost_env.zoho.search_and_download_vendor.return_value = {
            "found": True,
            "downloaded": 1,
            "saved_paths": [tmp_path / "x.pdf"],
        }
        orch._step_4_cost_invoices()
        cost_env.validate.assert_not_called()
        assert orch.report.cost_found_vendors == {"Shop": "Zoho Mail"}


# ── Step 5 / 6 / 7 extra branches ───────────────────────────────────────────


class TestStep5Blob:
    def test_statement_found_in_blob(self, tmp_path):
        orch = _orch(tmp_path)
        orch.month_dir.mkdir(parents=True)
        orch.storage.list_files.return_value = [
            _blob("faktury/2025/czerwiec/notes.txt"),
            _blob("faktury/2025/czerwiec/PKO_06.PDF"),
        ]
        orch._step_5_bank_statement()
        assert orch.report.bank_statement_found is True

    def test_blob_error_means_missing(self, tmp_path):
        orch = _orch(tmp_path)
        orch.month_dir.mkdir(parents=True)
        orch.storage.list_files.side_effect = RuntimeError("down")
        orch._step_5_bank_statement()
        assert orch.report.bank_statement_found is False
        assert any("PKO" in w for w in orch.report.warnings)


class TestStep6ZipUploadFailure:
    def test_local_zip_upload_failure_is_warning(self, tmp_path):
        import zipfile

        orch = _orch(tmp_path)
        orch.month_dir.mkdir(parents=True)
        local_zip = orch.month_dir / "czerwiec_2025_HUMIO.zip"
        with zipfile.ZipFile(local_zip, "w") as zf:
            zf.writestr("a.pdf", b"x")
        orch.storage.upload.side_effect = OSError("blob down")
        with (
            patch(f"{MOD}.create_month_archive_from_blob", side_effect=RuntimeError("x")),
            patch(f"{MOD}.create_month_archive", return_value=local_zip),
        ):
            orch._step_6_zip_archive()
        assert orch.report.zip_files == ["a.pdf"]
        assert any("ZIP blob upload failed" in w for w in orch.report.warnings)
        assert "ZIP archive" in orch.report.steps_completed


class TestIncludeScopedInbox:
    def test_copies_period_inbox_into_month_tree(self, tmp_path):
        orch = _orch(tmp_path)
        prefix = "faktury/inbox/2025-06/"
        orch.storage.list_files.return_value = [
            _blob(prefix + "sub/a.pdf"),
            _blob(prefix + "../evil.pdf"),
            _blob(prefix),
        ]
        orch.storage.download.side_effect = lambda key, dest: Path(dest).write_bytes(b"data")

        orch._include_scoped_inbox_in_month()

        orch.storage.list_files.assert_called_once_with(prefix)
        orch.storage.download.assert_called_once()
        assert orch.storage.download.call_args.args[0] == prefix + "sub/a.pdf"
        assert orch.storage.upload.call_args.args[1] == f"{orch._blob_prefix}/inbox/sub/a.pdf"
        assert (orch.month_dir / "inbox" / "sub" / "a.pdf").read_bytes() == b"data"

    def test_custom_prefix_used_verbatim(self, tmp_path):
        orch = _orch(tmp_path, inbox_prefix="custom/inbox/")
        orch._include_scoped_inbox_in_month()
        orch.storage.list_files.assert_called_once_with("custom/inbox/")
        orch.storage.download.assert_not_called()


class TestStep7BlobZip:
    @patch(f"{MOD}.EmailService")
    @patch.object(MonthCloseOrchestrator, "_get_secret", return_value="pw")
    def test_downloads_blob_zip_and_resets_state(self, _secret, email_cls, tmp_path):
        orch = _orch(tmp_path)
        orch.report.zip_path = Path("faktury/2025/czerwiec/czerwiec_2025_HUMIO.zip")
        seen: dict[str, Path] = {}

        def download(key, dest):
            Path(dest).write_bytes(b"PK")
            seen["dest"] = Path(dest)

        orch.storage.download.side_effect = download
        svc = email_cls.return_value

        def send_report(**kwargs):
            assert kwargs["attachments"][0].exists()

        svc.send_report.side_effect = send_report

        orch._step_7_email()

        assert orch.storage.download.call_args.args[0] == str(orch.report.zip_path)
        attached = svc.send_report.call_args.kwargs["attachments"][0]
        assert attached == seen["dest"]
        assert not attached.parent.exists()  # temp dir cleaned up
        assert orch.report.email_sent is True
        orch.state.reset.assert_called_once()

    @patch(f"{MOD}.EmailService")
    @patch.object(MonthCloseOrchestrator, "_get_secret", return_value="pw")
    def test_temp_dir_cleaned_when_send_fails(self, _secret, email_cls, tmp_path):
        orch = _orch(tmp_path, manage_state=False)
        orch.report.zip_path = Path("blob/x.zip")
        orch.storage.download.side_effect = lambda key, dest: Path(dest).write_bytes(b"PK")
        email_cls.return_value.send_report.side_effect = RuntimeError("smtp")
        with pytest.raises(RuntimeError, match="smtp"):
            orch._step_7_email()
        attached = email_cls.return_value.send_report.call_args.kwargs["attachments"][0]
        assert not attached.parent.exists()
        assert orch.report.email_sent is False
        orch.state.reset.assert_not_called()
