from py_project.main import main


def test_main(capsys) -> None:
    main()
    assert capsys.readouterr().out == "Hello from py-project!\n"
