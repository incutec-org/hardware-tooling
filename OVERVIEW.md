# Incutec hardware tooling

## Position in the workspace

```mermaid
%%{init: {"theme": "base", "themeVariables": {"background": "#0b1120", "lineColor": "#94a3b8", "fontFamily": "Inter, ui-sans-serif, system-ui"}, "flowchart": {"curve": "linear", "nodeSpacing": 30, "rankSpacing": 44}}}%%
flowchart LR
    HW["OpenDrone/hardware/&lt;Board&gt;<br/>KiCad project · AGENTS.md"]
    TPL["OpenDrone/_template/AGENTS.md<br/>Rules section"]
    ORG["OpenDrone/_org-github/engineering/<br/>approved-violations.json<br/>model-fixes.json"]
    SCR["scripts/<br/>stateless transforms<br/>no product · release · supplier data"]
    PROD["production/adapters/kicad/handoff_pack.py<br/>imports quote_pack · runs gerber_check"]
    BRAND["OpenDrone/brand/packaging/<br/>packaging_art.py"]
    OUT["BOARD-LOCAL OUTPUTS<br/>production/ · export/ · images/"]

    HW -->|"board + schematic in"| SCR
    TPL -->|"agents_section_sync.py<br/>stamp Rules into */AGENTS.md"| HW
    ORG -.->|"--approved-violations<br/>--fixes"| SCR
    SCR -->|"renders · STEP · fab set<br/>BOM · reports"| OUT
    SCR -->|"INCUTEC_KICAD_TOOLS<br/>quote_pack · gerber_check"| PROD
    SCR -.->|"flat vector art"| BRAND

    classDef design fill:#0c4a6e,stroke:#38bdf8,color:#f8fafc,stroke-width:2px;
    classDef policy fill:#27272a,stroke:#f8fafc,color:#f8fafc,stroke-width:2px;
    classDef tool fill:#1e293b,stroke:#f8fafc,color:#f8fafc,stroke-width:2.5px;
    classDef production fill:#78350f,stroke:#f59e0b,color:#fff7ed,stroke-width:2px;
    classDef out fill:#134e4a,stroke:#2dd4bf,color:#f0fdfa,stroke-width:2px;
    classDef brand fill:#27272a,stroke:#a1a1aa,color:#fafafa,stroke-width:1.5px;

    class HW design;
    class TPL,ORG policy;
    class SCR tool;
    class PROD production;
    class OUT out;
    class BRAND brand;
```

## Tool map

```mermaid
%%{init: {"theme": "base", "themeVariables": {"background": "#0b1120", "lineColor": "#64748b", "fontFamily": "Inter, ui-sans-serif, system-ui"}, "flowchart": {"curve": "basis", "nodeSpacing": 22, "rankSpacing": 34}}}%%
flowchart TB
    ROOT["scripts/"]

    ROOT --> META
    ROOT --> KICAD
    ROOT --> REL
    ROOT --> TPL

    subgraph META["REPOSITORY TOOLING"]
        direction LR
        A1["hardware/agents_section_sync.py<br/>stamp or --check one ## section"]
        A2["tests/<br/>pytest · section sync · connectivity · release"]
    end

    subgraph KICAD["hardware/kicad/"]
        direction TB

        subgraph INSPECT["INSPECTION"]
            direction LR
            I1["netlist_extract.py"]
            I2["pcb_extract.py"]
            I3["connectivity_report.py"]
            I4["check_models.py · E1–E5<br/>KPY"]
            I5["check_export.py · C1–C3"]
        end

        subgraph FAB["MANUFACTURING DATA"]
            direction LR
            F1["fab_export.py · KPY<br/>Fabrication Toolkit → production/"]
            F2["universal_bom.py · KPY"]
            F3["quote_pack.py · KPY<br/>per-supplier BOM + positions"]
            F4["portal_gerbers.py<br/>strip attributes · rename outline"]
            F5["gerber_check.py<br/>DFM on the zip the fab gets"]
            F6["assembly_drawing.py · KPY"]
            F7["import_part.py · KPY<br/>LCSC via easyeda2kicad + repairs"]
            F8["add_mpn_fields.py · kicad-skip<br/>set_edgecuts_width.py"]
        end

        subgraph MEDIA["IMAGES + CAD"]
            direction LR
            M1["render_board.py · KPY<br/>transparent PNG · temp copy"]
            M2["dimension_overlay.py<br/>image only"]
            M3["packaging_art.py · KPY<br/>flat vector SVG"]
            M4["export_step.py · KPY + OCP<br/>→ step_post.py system python"]
            M5["wrl_to_step.py · model_audit.py<br/>apply_models.py"]
            M6["multiboard/<br/>Kicad-Multi-PCB fork · install.sh"]
        end
    end

    subgraph REL["hardware/release/"]
        direction LR
        R1["kicad_release.py<br/>G1–G5 orchestrator<br/>INCUTEC_KICAD_TOOLS · KICAD_CLI"]
    end

    subgraph TPL["templates/hardware-repository/"]
        direction LR
        T1["AGENTS.md · README.md"]
        T2["release/approved-violations.json<br/>schema 1 · boards {}"]
        T3["hardware/tools/ · images/"]
    end

    classDef root fill:#111827,stroke:#f8fafc,color:#f8fafc,stroke-width:2.5px;
    classDef meta fill:#1e293b,stroke:#94a3b8,color:#f8fafc,stroke-width:1.5px;
    classDef inspect fill:#27272a,stroke:#f8fafc,color:#f8fafc,stroke-width:2px;
    classDef fab fill:#78350f,stroke:#f59e0b,color:#fff7ed,stroke-width:2px;
    classDef media fill:#134e4a,stroke:#2dd4bf,color:#f0fdfa,stroke-width:2px;
    classDef rel fill:#581c87,stroke:#c084fc,color:#faf5ff,stroke-width:2px;
    classDef tpl fill:#0c4a6e,stroke:#38bdf8,color:#f8fafc,stroke-width:2px;

    class ROOT root;
    class A1,A2 meta;
    class I1,I2,I3,I4,I5 inspect;
    class F1,F2,F3,F4,F5,F6,F7,F8 fab;
    class M1,M2,M3,M4,M5,M6 media;
    class R1 rel;
    class T1,T2,T3 tpl;

    style META fill:transparent,stroke:#475569,color:#cbd5e1;
    style KICAD fill:transparent,stroke:#475569,color:#cbd5e1;
    style INSPECT fill:transparent,stroke:#64748b,color:#e2e8f0;
    style FAB fill:transparent,stroke:#b45309,color:#fed7aa;
    style MEDIA fill:transparent,stroke:#0f766e,color:#99f6e4;
    style REL fill:transparent,stroke:#7e22ce,color:#e9d5ff;
    style TPL fill:transparent,stroke:#0369a1,color:#bae6fd;
```

## Release gate chain

```mermaid
%%{init: {"theme": "base", "themeVariables": {"background": "#0b1120", "lineColor": "#cbd5e1", "fontFamily": "Inter, ui-sans-serif, system-ui"}, "flowchart": {"curve": "linear", "nodeSpacing": 26, "rankSpacing": 44}}}%%
flowchart LR
    IN["board.kicad_pcb + .kicad_sch<br/>approved-violations.json"]
    IN --> G1{"G1 · ERC + DRC<br/>≤ approved max per type<br/>new type blocks"}
    G1 -->|"pass"| G2{"G2 · check_models.py<br/>no blocking 3D findings"}
    G2 -->|"pass"| G3{"G3 · fab set<br/>quote_pack + check_export"}
    G3 -->|"pass"| G4{"G4 · export_step.py<br/>STEP"}
    G4 -->|"pass"| G5{"G5 · schematic PDF"}
    G5 -->|"pass"| PREP["RELEASE PREPARED<br/>export/ · production/"]
    PREP -.->|"separate human actions"| PUB["tag · GitHub release<br/>storefront · order"]

    G1 -->|"fail"| STOP["blocked until<br/>maintainer review"]
    G2 -->|"fail"| STOP
    G3 -->|"fail"| STOP
    G4 -->|"fail"| STOP
    G5 -->|"fail"| STOP

    classDef input fill:#0c4a6e,stroke:#38bdf8,color:#f8fafc,stroke-width:2px;
    classDef gate fill:#27272a,stroke:#f8fafc,color:#f8fafc,stroke-width:2px;
    classDef out fill:#134e4a,stroke:#2dd4bf,color:#f0fdfa,stroke-width:2px;
    classDef human fill:#581c87,stroke:#c084fc,color:#faf5ff,stroke-width:2px;
    classDef fail fill:#7f1d1d,stroke:#f87171,color:#fef2f2,stroke-width:2px;

    class IN input;
    class G1,G2,G3,G4,G5 gate;
    class PREP out;
    class PUB human;
    class STOP fail;
```

## Manufacturing data path

```mermaid
%%{init: {"theme": "base", "themeVariables": {"background": "#0b1120", "lineColor": "#cbd5e1", "fontFamily": "Inter, ui-sans-serif, system-ui"}, "flowchart": {"curve": "linear", "nodeSpacing": 26, "rankSpacing": 44}}}%%
flowchart LR
    PCB["board.kicad_pcb<br/>fabrication-toolkit-options.json"]
    SCH["board.kicad_sch<br/>MPN · Manufacturer fields"]

    PCB --> FT["fab_export.py<br/>Fabrication Toolkit headless"]
    PCB --> UB["universal_bom.py"]
    SCH -.->|"fill MPN gaps"| UB
    UB --> QP["quote_pack.py<br/>production/quote-pack-&lt;rev&gt;/<br/>per-supplier BOM + CPL"]
    FT --> QP
    FT --> PG["portal_gerbers.py<br/>&lt;stem&gt;_portal.zip"]
    PG --> GC{"gerber_check.py<br/>min track · min drill · DRC"}
    QP --> CE{"check_export.py<br/>C1–C3 vs netlist"}
    GC -->|"pass"| HAND["production/ handoff_pack.py<br/>controlled supplier pack"]
    CE -->|"pass"| HAND

    LC["LCSC part number"] --> IP["import_part.py<br/>symbol · footprint · 3D<br/>six repairs"]
    IP --> LIB["board lib or KiCad-Library"]
    LIB -.-> SCH

    classDef input fill:#0c4a6e,stroke:#38bdf8,color:#f8fafc,stroke-width:2px;
    classDef tool fill:#1e293b,stroke:#94a3b8,color:#f8fafc,stroke-width:1.5px;
    classDef gate fill:#27272a,stroke:#f8fafc,color:#f8fafc,stroke-width:2px;
    classDef production fill:#78350f,stroke:#f59e0b,color:#fff7ed,stroke-width:2px;

    class PCB,SCH,LC,LIB input;
    class FT,UB,QP,PG,IP tool;
    class GC,CE gate;
    class HAND production;
```

## Interpreter rule

```mermaid
%%{init: {"theme": "base", "themeVariables": {"background": "#0b1120", "lineColor": "#94a3b8", "fontFamily": "Inter, ui-sans-serif, system-ui"}, "flowchart": {"curve": "linear", "nodeSpacing": 34, "rankSpacing": 40}}}%%
flowchart LR
    Q{"IMPORTS pcbnew?"}
    Q -->|"yes"| KPY["KiCad bundled Python · $KPY<br/>close KiCad first<br/>check_models · fab_export · universal_bom<br/>quote_pack · assembly_drawing · import_part<br/>render_board · packaging_art · export_step · apply_models"]
    Q -->|"no"| SYS["system python3<br/>netlist_extract · pcb_extract · connectivity_report<br/>check_export · portal_gerbers · gerber_check<br/>dimension_overlay · step_post · wrl_to_step<br/>agents_section_sync · kicad_release"]
    V["python3 -m pytest tests/<br/>repository validation"]

    classDef gate fill:#27272a,stroke:#f8fafc,color:#f8fafc,stroke-width:2px;
    classDef kpy fill:#0c4a6e,stroke:#38bdf8,color:#f8fafc,stroke-width:2px;
    classDef sys fill:#1e293b,stroke:#94a3b8,color:#f8fafc,stroke-width:1.5px;
    classDef ok fill:#134e4a,stroke:#2dd4bf,color:#f0fdfa,stroke-width:2px;

    class Q gate;
    class KPY kpy;
    class SYS sys;
    class V ok;
```
