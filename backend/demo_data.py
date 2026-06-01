from backend.main import create_demo_reports, init_db, sync_local_folder


def main() -> None:
    init_db()
    created = create_demo_reports()
    sync = sync_local_folder()
    print("Created demo reports:")
    for company in created["created_companies"]:
        print(f"- {company}")
    print(f"Input folder: {created['input_dir']}")
    print(f"Discovered documents: {sync['discovered_documents']}")


if __name__ == "__main__":
    main()
