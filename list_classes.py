from vision.model_config import MODEL_GROUPS
from ultralytics import YOLO


errors = []
for group_name, models in MODEL_GROUPS.items():
    print(f"\n=== {group_name} ===")
    for entry in models:
        path = entry["path"]
        expected = tuple(entry.get("classes", ()))
        try:
            model = YOLO(path)
            actual = tuple(
                str(model.names[index])
                for index in sorted(model.names)
            )
            print(f"  {path}")
            for cls_id, cls_name in model.names.items():
                print(f"      [{cls_id}] {cls_name}")
            if expected and actual != expected:
                errors.append(
                    f"{path}: actual={actual}, expected={expected}"
                )
                print(f"      CLASS MISMATCH: expected {expected}")
            else:
                print("      CLASS CONTRACT: OK")
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            print(f"  {path}  ERROR: {exc}")

if errors:
    print("\nMODEL CLASS CHECK FAILED")
    for error in errors:
        print(f"  {error}")
    raise SystemExit(1)

print("\nMODEL CLASS CHECK PASSED")
