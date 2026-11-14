# public-gate.pl LITERALS ALLOW ROOT MSGFILE: report every place a forbidden literal survives in an exported
# tree (ROOT) or in the commit text (MSGFILE). Case-insensitive, fixed strings. ALLOW holds exact strings
# that are cut out of a line BEFORE the match (e.g. a stored method name), so an allowed form passes while
# anything else on the line still fails. Prints file:line only, never the literal. Exit 1 on any hit.
use strict;
use warnings;
use File::Find;

my ($lits, $allow, $root, $msg) = @ARGV;
sub lines_of {
    my ($f) = @_;
    open(my $h, '<:raw', $f) or die "public-gate: cannot read $f\n";
    my @l = grep { /\S/ } map { s/\r?\n\z//r } <$h>;
    close $h;
    return @l;
}
my @pat = map { lc } lines_of($lits);
die "public-gate: the literal list is empty\n" unless @pat;
my @ok = $allow ne '' ? map { lc } lines_of($allow) : ();
@ok = sort { length($b) <=> length($a) } @ok;    # longest first, so a longer allowed form wins

my $hits = 0;
sub scan {
    my ($label, $text) = @_;
    my $n = 0;
    for my $line (split /\n/, $text, -1) {
        $n++;
        my $l = lc $line;
        for my $w (@ok) { $l =~ s/\Q$w\E//g }
        for my $p (@pat) {
            if (index($l, $p) >= 0) { print "$label:$n\n"; $hits++; last }
        }
    }
}
my @packed;
find({ no_chdir => 1, wanted => sub {
    return unless -f $_;
    my $rel = substr($_, length($root) + 1);
    if ($rel =~ /\.(gz|tgz|zip|xz|bz2|7z|zst|tar|whl|jar)\z/i) { push @packed, $rel; return }
    open(my $h, '<:raw', $_) or die "public-gate: cannot read $rel\n";
    local $/; my $b = <$h>; close $h;
    scan($rel, $b);
} }, $root);
open(my $m, '<:raw', $msg) or die "public-gate: cannot read the commit text\n";
{ local $/; scan('(commit message/author)', scalar <$m>) }
for my $p (@packed) { print "$p: a compressed file cannot be inspected; refusing\n"; $hits++ }
exit($hits ? 1 : 0);
