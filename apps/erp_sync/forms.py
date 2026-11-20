from django import forms

MAX_CSV_BYTES = 5 * 1024 * 1024


class CsvImportForm(forms.Form):
    file = forms.FileField(
        label="Plik CSV z kontrahentami",
        help_text="Eksport z enova365 lub Excela (separator ; lub ,), maks. 5 MB.",
        widget=forms.ClearableFileInput(attrs={"class": "file-input file-input-bordered w-full", "accept": ".csv,text/csv"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if not upload.name.lower().endswith(".csv"):
            raise forms.ValidationError("Wybierz plik z rozszerzeniem .csv.")
        if upload.size > MAX_CSV_BYTES:
            raise forms.ValidationError("Plik jest za duży (maks. 5 MB).")
        if upload.size == 0:
            raise forms.ValidationError("Plik jest pusty.")
        return upload
