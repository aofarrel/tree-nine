print("VERSION 1.4.9")
script_path = '/scripts/cluster_main_script.py'

import os
import argparse
import logging
from datetime import date
import numpy as np
import ete3
import tqdm as progressbar

parser = argparse.ArgumentParser()
parser.add_argument('mat_tree', type=str, help='input MAT')
parser.add_argument('nwk_tree', type=str, help='input nwk')
parser.add_argument('-s', '--samples', required=False, type=str,help='comma separated list of samples')
parser.add_argument('-d', '--distance', default=20, type=int, help='max distance between samples to identify as clustered')
parser.add_argument('-rd', '--recursive-distance', type=lambda x: [int(i) for i in x.split(',')], default=[10, 5], help='after identifying --distance cluster, search for subclusters with this distance')

parser.add_argument('-t', '--type', choices=['BM', 'NB'], type=str.upper, help='BM=backmasked, NB=not-backmasked; will add BM/NB before prefix')
parser.add_argument('-bo', '--bigmatrixout', type=str, help='outname for the big matrix (sans ext)')
parser.add_argument('-sf', '--startfrom', default=0, type=int, help='the six-digit int part of cluster UUIDs will begin with the next integer after this one')

parser.add_argument('-nl', '--nolonely', action='store_true', help='do not make a subtree for unclustered samples (useful if recursing)')
parser.add_argument('-neo', '--noextraouts', action='store_true', help='do not write extra summary information to their own file (useful if recursing)')

parser.add_argument('-v', '--verbose', action='store_true', help='enable info logging')
parser.add_argument('-vv', '--veryverbose', action='store_true', help='enable debug logging')

parser.add_argument('-nc', '--nocluster', action='store_true', help='matrix, but do not search for clusters (useful if recursing)')
parser.add_argument('-c', '--contextsamples', default=0, type=int, help='number of context samples to add per cluster (appears only in nwk)')
parser.add_argument('-m', '--microreact', action='store_true', help='run microreact_script.py per cluster')

args = parser.parse_args()
if args.veryverbose:
    logging.basicConfig(level=logging.DEBUG)
elif args.verbose:
    logging.basicConfig(level=logging.INFO)
else:
    logging.basicConfig(level=logging.WARNING)
t = ete3.Tree(args.nwk_tree, format=1)
samps = args.samples.split(',') if args.samples else sorted([leaf.name for leaf in t])
if args.type == 'BM':
    is_backmasked, type_prefix, args_dot_type = True, 'BM_', '-t BM'
elif args.type == 'NB':
    is_backmasked, type_prefix, args_dot_type = False, 'NB_', '-t NB'
else:
    is_backmasked, type_prefix, args_dot_type = None, '', ''
prefix = type_prefix
big_matrix = args.bigmatrixout if args.bigmatrixout else f'{prefix}_big_dmtrx_big'  # PURPOSELY calling it big-big to avoid WDL globbing B.S. with the non-big dmatrices

def handle_subprocess(explainer, system_call_as_string):
    logging.info(explainer)
    logging.info(system_call_as_string) # pylint: disable=W1203
    os.system(system_call_as_string)

def path_to_root(ete_tree, node_name):
    # Browse the tree from a specific leaf to the root
    logging.debug("Getting path for %s in %s", node_name, type(ete_tree))
    node = ete_tree.search_nodes(name=node_name)[0]
    logging.debug("Node as found in ete tree: %s", node)
    path = [node]
    while node:
        node = node.up
        path.append(node)
    logging.debug("path for %s: %s", node_name, path)
    return path

def dist_matrix(tree_to_matrix, samples):
    samp_ancs = {}
    #samp_dist = {}
    neighbors = []
    unclustered = set()
    
    #for each input sample, find path to root and branch lengths
    for sample in progressbar.tqdm(samples, desc="Finding roots and branch lengths"):
        s_ancs = path_to_root(tree_to_matrix, sample)
        samp_ancs[sample] = s_ancs
    
    #create matrix for samples
    matrix = np.full((len(samples),len(samples)), -1)

    for i in progressbar.trange(len(samples), desc="Creating matrix"): # trange is a tqdm optimized version of range
        this_samp = samples[i]
        definitely_in_a_cluster = False
        logging.debug("Checking %s", this_samp)

        for j in range(len(samples)):
            that_samp = samples[j]
            #Future goal: add catch to prevent reiteration of already checked pairs
            if that_samp == this_samp: # self-to-self
                matrix[i][j] = '0'
            elif matrix[i][j] == -1: # ie, we haven't calculated this one yet
                #find lca, add up branch lengths 
                this_path = 0
                that_path = 0
                
                for a in samp_ancs[this_samp]:
                    this_path += a.dist
                    if a in samp_ancs[that_samp]:
                        lca = a
                        this_path -= a.dist
                        #logging.debug(f"  found a in samp_ancs[that_samp], setting this_path")
                        break
                
                for a in samp_ancs[that_samp]:
                    that_path += a.dist
                    if a == lca:
                        #logging.debug(f'  a == lca, setting that_path')
                        that_path -= a.dist
                        break
                
                logging.debug("  sample %s vs other sample %s: this_path %s, that_path %s", this_samp, that_samp, this_path, that_path)
                total_distance = int(this_path + that_path)
                matrix[i][j] = total_distance
                matrix[j][i] = total_distance
                if not args.nocluster and total_distance <= args.distance:
                    logging.debug("  %s and %s seem to be in a cluster (%s)", this_samp, that_samp, total_distance)
                    neighbors.append(tuple((this_samp, that_samp)))
                    definitely_in_a_cluster = True
        
        # after iterating through all of j, if this sample is not in a cluster, make note of that
        if not args.nocluster and not definitely_in_a_cluster:
            logging.debug("  %s is either not in a cluster or clustered early", this_samp)
            #logging.debug(matrix[i])
            second_smallest_distance = np.partition(matrix[i], 1)[1] # second smallest, because smallest is self-self at 0
            if second_smallest_distance <= args.distance:
                logging.debug("  Oops, %s was already clustered! (closest sample is %s SNPs away)", this_samp, second_smallest_distance)
            else:
                logging.debug("  %s appears to be truly unclustered (closest sample is %s SNPs away)", this_samp, second_smallest_distance)
                unclustered.add(this_samp)
    
    # finished iterating, let's see what our clusters look like
    if not args.nocluster:
        true_clusters = []
        first_iter = True
        for pairs in neighbors:
            existing_cluster = False
            if first_iter:
                true_clusters.append(set([pairs[0], pairs[1]]))
            else:
                for sublist in true_clusters:
                    if pairs[0] in sublist:
                        sublist.add(pairs[1])
                        existing_cluster = True
                    if pairs[1] in sublist: # NOT ELSE IF
                        sublist.add(pairs[0])
                        existing_cluster = True
                if not existing_cluster:
                    true_clusters.append(set([pairs[0], pairs[1]]))
            first_iter = False
    if args.nocluster:
        true_clusters = None
    logging.debug("Returning:\n\tsamples:\n%s\n\tmatrix:\n%s\n\ttrue_clusters:\n%s\n\tunclustered:\n%s", samples, matrix, true_clusters, unclustered)
    return samples, matrix, true_clusters, unclustered

samps, mat, clusters, lonely = dist_matrix(t, samps)
total_samples_processed = len(samps)
logging.info("Processed %s samples", total_samples_processed)
logging.debug("Samples processed: %s", samps) # check if alphabetized

#for i in range(len(mat)):
#    for j in range(len(mat[i])):
#        if mat[i][j] != mat[j][i]:
#            print(i,j)

with open(f"{big_matrix}.tsv", "a", encoding="utf-8") as outfile:
    outfile.write('sample\t'+'\t'.join(samps))
    outfile.write("\n")
    for k in range(len(samps)): # don't change to enumerate without changing i; with enumerate it's a tuple
        #strng = np.array2string(mat[i], separator='\t')[1:-1]
        line = [ str(int(count)) for count in mat[k]]
        outfile.write(f'{samps[k]}\t' + '\t'.join(line) + '\n')

# this could probably be made more efficient
if not args.nocluster:

    logging.info("Clustering...")

    # sample_cluster is the Nextstrain-style TSV used for annotation, eg:
    # sample12    cluster1
    # sample13    cluster1
    # sample14    cluster1
    sample_cluster = ['Sample\tCluster\n']
    sample_clusterUUID = ['Sample\tClusterUUID\n']

    # cluster_samples is for matutils extract to generate Nextstrain subtrees, eg:
    # cluster1    sample12,sample13,sample14
    cluster_samples = ['Cluster\tSamples\n']

    # summary information for humans to look at
    n_clusters = len(clusters) # immutable
    n_samples_in_clusters = 0  # mutable
    
    for n in range(n_clusters):
        # get basic information -- we can safely sort here as do not use the array directly
        samples_in_cluster = sorted(list(clusters[n]))
        assert len(samples_in_cluster) == len(set(samples_in_cluster))
        n_samples_in_clusters += len(samples_in_cluster) # samples in ANY cluster, not just this one
        samples_in_cluster_str = ",".join(samples_in_cluster)
        is_cdph = is_cdph = any(
            samp_name[:2].isdigit() or 
            (samp_name.startswith("[BM]") and samp_name[4:6].isdigit()) or
            (samp_name.startswith("[NB]") and samp_name[4:6].isdigit())
            for samp_name in samples_in_cluster
        )
        locale = 'CA' if is_cdph else '??'
        if prefix == 'BM':
            short_prefix = 'm'
        elif prefix == 'NB':
            short_prefix = 'n'
        else:
            short_prefix = prefix
        number_part = n + args.startfrom
        UUID = f"{str(args.distance).zfill(2)}SNP-CA-{str(date.today().year)}-{short_prefix}{str(number_part).zfill(6)}"
        cluster_name = UUID # previously they were meaningfully different, but this is less prone to nonsense
        logging.info("Identified %s with %s members", cluster_name, len(samples_in_cluster))
        with open("cluster_information.txt", "a", encoding="utf-8") as cluster_information_file:
            cluster_information_file.write(f'{cluster_name} has {n_samples_in_clusters}\n') # TODO: eventually add old/new samp information

        # build cluster_samples line for this cluster
        cluster_samples.append(f"{cluster_name}\t{samples_in_cluster_str}\n")

        if len(args.recursive_distance) == 0:
            handle_subprocess(f"Generating {cluster_name}'s distance matrix...", f"python3 {script_path} '{args.mat_tree}' '{args.nwk_tree}' -s{samples_in_cluster_str} -v {args_dot_type} -nc -nl -bo {prefix}_{cluster_name}")
        else:
            next_next_recursion = '' if len(args.recursive_distance) == 1 else f'-rd {",".join(map(str, args.recursive_distance))}'
            handle_subprocess(f"Looking for {args.recursive_distance[0]}-SNP subclusters...", f"python3 {script_path} '{args.mat_tree}' '{args.nwk_tree}' -s{samples_in_cluster_str} -v {args_dot_type} -nl -bo {prefix}_{cluster_name} -sf {number_part+1} {next_next_recursion}")
        
        # build sample_cluster lines for this cluster - this will be used for auspice annotation
        for s in samples_in_cluster:
            sample_cluster.append(f"{s}\t{cluster_name}\n")
            sample_clusterUUID.append(f"{s}\t{UUID}\n")

        # run matUtils extract to make cluster subtree
        minimum_tree_size = args.contextsamples + len(samples_in_cluster)
        with open("this_cluster_samples.txt", "w", encoding="utf-8") as temp: # gets overwritten each time
            temp.write(f"{cluster_name}\t{samples_in_cluster_str}")

        # TODO: restore metadata in the JSON version of the tree, -M metadata_tsv
        handle_subprocess("Calling matUtils to extract nwk...",
            f'matUtils extract -i "{args.mat_tree}" -t "{prefix}_{cluster_name}" -s this_cluster_samples.txt -N {minimum_tree_size}')
        handle_subprocess("Calling matUtils to extract JSON...",
            f'matUtils extract -i "{args.mat_tree}" -j "{prefix}" -s this_cluster_samples.txt -N {minimum_tree_size}')

        # for some reason, nwk subtrees seem to end up with .nw as their extension
        print("Workdir as current")
        print(os.listdir('.'))
        os.rename(f"{prefix}.nw", f"{prefix}.nwk")
        print("Workdir, renamed nwk")
        print(os.listdir('.'))

        if args.microreact:
            handle_subprocess(f"Uploading {cluster_name} to MR...",
                f"python3 scripts/microreact.py {cluster_name} {prefix}_{cluster_name}.nwk {prefix}_{cluster_name}_dmtrx.tsv")
    
    # add in the unclustered samples (outside for loop to avoid writing multiple times)
    # however, don't add to the UUID list, or else persistent cluster IDs will break
    if not args.nl:
        lonely = sorted(list(lonely))
        for george in lonely: # W0621, https://en.wikipedia.org/wiki/Lonesome_George
            sample_cluster.append(f"{george}\tlonely\n")
        with open("LONELY.txt", "a", encoding="utf-8") as unclustered_samples_list:
            unclustered_samples_list.writelines(lonely)
        unclustered_as_str = ','.join(lonely)
        cluster_samples.append(f"lonely\t{unclustered_as_str}\n")
        handle_subprocess("Also extracting a tree for lonely samples...",
            f'matUtils extract -i "{args.mat_tree}" -t "LONELY" -s {prefix}_lonely.txt -N {minimum_tree_size}')
        os.rename("LONELY.nw", "LONELY.nwk")
        print(os.listdir('.'))
    
    # auspice-style TSV for annotation of clusters
    with open(f"{prefix}_cluster_annotation.tsv", "a", encoding="utf-8") as samples_for_annotation:
        samples_for_annotation.writelines(sample_cluster)

    # auspice-style TSV with cluster UUIDs instead of full names; used for persistent cluster IDs
    # this one should never include unclustered samples
    with open(f"{prefix}_cluster_UUIDs.tsv", "a", encoding="utf-8") as samples_by_cluster_UUID:
        samples_by_cluster_UUID.writelines(sample_clusterUUID)
    
    # usher-style TSV for subtree extraction
    # note that because we are recursing, samples can have more than one subtree assignment, so this can't be fed into usher directly
    with open(f"{prefix}_cluster_extraction.tsv", "a", encoding="utf-8") as clusters_for_subtrees:
        cluster_samples.append("\n") # to avoid skipping last line when read
        clusters_for_subtrees.writelines(cluster_samples)
    
    # generate little summary files for WDL to parse directly
    if not args.neo:
        with open("n_clusters", "w", encoding="utf-8") as n_cluster: n_cluster.write(str(n_clusters))
        with open("n_samples_in_clusters", "w", encoding="utf-8") as n_cluded: n_cluded.write(str(n_samples_in_clusters))
        with open("n_samples_processed", "w", encoding="utf-8") as n_processed: n_processed.write(str(total_samples_processed))
        with open("n_unclustered", "w", encoding="utf-8") as n_lonely: n_lonely.write(str(len(lonely)))
